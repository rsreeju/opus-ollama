import contextlib
import io
import json
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import ollama_code as oc


def write_cfg(tmp, cfg):
    p = Path(tmp) / "config.json"
    p.write_text(json.dumps(cfg))
    return p


class ConfigTests(unittest.TestCase):
    def test_load_applies_defaults(self):
        with tempfile.TemporaryDirectory() as t:
            p = write_cfg(t, {"profiles": {"a": {"model": "m"}}})
            cfg = oc.load_config(p)
            self.assertEqual(cfg["timeout_sec"], 300)
            self.assertEqual(cfg["max_output_tokens"], 8192)

    def test_load_missing_file(self):
        with self.assertRaises(oc.WorkerError):
            oc.load_config("/nonexistent/config.json")

    def test_load_bad_json(self):
        with tempfile.TemporaryDirectory() as t:
            p = Path(t) / "config.json"
            p.write_text("{nope")
            with self.assertRaises(oc.WorkerError):
                oc.load_config(p)

    def test_load_no_profiles(self):
        with tempfile.TemporaryDirectory() as t:
            with self.assertRaises(oc.WorkerError):
                oc.load_config(write_cfg(t, {"profiles": {}}))

    def test_resolve_default_and_named(self):
        cfg = {"default_profile": "a", "profiles": {
            "a": {"model": "m1"}, "b": {"model": "m2", "host": "http://h:1/"}}}
        self.assertEqual(oc.resolve_profile(cfg), ("a", {"model": "m1", "host": "http://localhost:11434"}))
        self.assertEqual(oc.resolve_profile(cfg, "b"), ("b", {"model": "m2", "host": "http://h:1"}))

    def test_resolve_unknown_profile(self):
        with self.assertRaisesRegex(oc.WorkerError, "unknown profile 'z'"):
            oc.resolve_profile({"profiles": {"a": {"model": "m"}}}, "z")

    def test_resolve_empty_model(self):
        with self.assertRaisesRegex(oc.WorkerError, "ollama list"):
            oc.resolve_profile({"profiles": {"a": {"model": ""}}}, "a")


class ParseTests(unittest.TestCase):
    def test_parses_blocks_ignoring_prose(self):
        reply = ("Sure! Here you go.\n\n### FILE: a.py\n```python\nprint(1)\n```\n\n"
                 "### FILE: dir/b.txt\n```\nhello\nworld\n```\nHope that helps!\n")
        self.assertEqual(oc.parse_blocks(reply),
                         [("a.py", "print(1)\n"), ("dir/b.txt", "hello\nworld\n")])

    def test_inner_fences_preserved(self):
        reply = "### FILE: README.md\n```markdown\n# T\n```bash\nls\n```\nend\n```\n"
        self.assertEqual(oc.parse_blocks(reply),
                         [("README.md", "# T\n```bash\nls\n```\nend\n")])

    def test_no_blocks(self):
        self.assertEqual(oc.parse_blocks("I cannot do that."), [])

    def test_header_without_fence_skipped(self):
        self.assertEqual(oc.parse_blocks("### FILE: a.py\nprint(1)\n"), [])


class ApplyTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.root = Path(self._t.name) / "proj"
        self.root.mkdir()
        self.addCleanup(self._t.cleanup)

    def test_writes_allowed_creates_dirs(self):
        w, r = oc.apply_blocks([("src/a.py", "x\n")], self.root, {"src/a.py"})
        self.assertEqual((self.root / "src/a.py").read_text(), "x\n")
        self.assertEqual(w, [("src/a.py", "", "x\n")])
        self.assertEqual(r, [])

    def test_overwrite_records_old(self):
        (self.root / "a.py").write_text("old\n")
        w, _ = oc.apply_blocks([("a.py", "new\n")], self.root, {"a.py"})
        self.assertEqual(w, [("a.py", "old\n", "new\n")])

    def test_not_allowlisted_rejected(self):
        w, r = oc.apply_blocks([("evil.py", "x\n")], self.root, {"a.py"})
        self.assertEqual(w, [])
        self.assertEqual(r[0][0], "evil.py")
        self.assertFalse((self.root / "evil.py").exists())

    def test_traversal_rejected_even_if_allowlisted(self):
        w, r = oc.apply_blocks([("../out.py", "x\n")], self.root, {"../out.py"})
        self.assertEqual(w, [])
        self.assertFalse((self.root.parent / "out.py").exists())
        self.assertEqual(len(r), 1)

    def test_absolute_rejected(self):
        target = str(self.root.parent / "abs.py")
        w, r = oc.apply_blocks([(target, "x\n")], self.root, {target})
        self.assertEqual(w, [])
        self.assertFalse(Path(target).exists())

    def test_symlink_escape_rejected(self):
        outside = Path(self._t.name) / "outside"
        outside.mkdir()
        (self.root / "link").symlink_to(outside)
        w, r = oc.apply_blocks([("link/x.py", "x\n")], self.root, {"link/x.py"})
        self.assertEqual(w, [])
        self.assertFalse((outside / "x.py").exists())

    def test_dot_slash_normalized(self):
        w, _ = oc.apply_blocks([("./a.py", "x\n")], self.root, {"a.py"})
        self.assertEqual([x[0] for x in w], ["a.py"])

    def test_report_contains_diff_and_rejections(self):
        rep = oc.format_report([("a.py", "old\n", "new\n")], [("b.py", "not in --write list")])
        self.assertIn("--- a/a.py", rep)
        self.assertIn("-old", rep)
        self.assertIn("+new", rep)
        self.assertIn("REJECTED b.py", rep)


class FakeOllama(BaseHTTPRequestHandler):
    reply = "### FILE: a.py\n```python\nprint(1)\n```\n"
    models = ["present:latest"]
    status = 200
    last_request = None
    last_auth = None

    def log_message(self, *a):
        pass

    def do_GET(self):
        FakeOllama.last_auth = self.headers.get("Authorization")
        body = json.dumps({"models": [{"name": n} for n in self.models]}).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        FakeOllama.last_auth = self.headers.get("Authorization")
        n = int(self.headers["Content-Length"])
        FakeOllama.last_request = json.loads(self.rfile.read(n))
        self.send_response(self.status)
        self.end_headers()
        self.wfile.write(json.dumps({"message": {"content": self.reply}}).encode())


def start_fake():
    srv = HTTPServer(("127.0.0.1", 0), FakeOllama)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.srv, self.host = start_fake()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        FakeOllama.reply = "### FILE: a.py\n```python\nprint(1)\n```\n"
        FakeOllama.models = ["present:latest"]
        FakeOllama.status = 200

    def test_chat_returns_text_and_sends_options(self):
        text = oc.chat(self.host, "present", [{"role": "user", "content": "hi"}], 5, 123)
        self.assertIn("### FILE: a.py", text)
        req = FakeOllama.last_request
        self.assertEqual(req["model"], "present")
        self.assertFalse(req["stream"])
        self.assertEqual(req["options"]["num_predict"], 123)

    def test_chat_empty_reply(self):
        FakeOllama.reply = "   "
        with self.assertRaisesRegex(oc.WorkerError, "empty reply"):
            oc.chat(self.host, "present", [], 5, 10)

    def test_chat_http_error(self):
        FakeOllama.status = 500
        with self.assertRaisesRegex(oc.WorkerError, "HTTP 500"):
            oc.chat(self.host, "present", [], 5, 10)

    def test_chat_unreachable(self):
        with self.assertRaisesRegex(oc.WorkerError, "cannot reach Ollama"):
            oc.chat("http://127.0.0.1:1", "m", [], 2, 10)

    def test_check_ok_with_implicit_latest(self):
        ok, msg = oc.check(self.host, "present")
        self.assertTrue(ok, msg)

    def test_check_missing_model_lists_installed_and_fix(self):
        ok, msg = oc.check(self.host, "absent")
        self.assertFalse(ok)
        self.assertIn("ollama pull absent", msg)
        self.assertIn("present:latest", msg)

    def test_check_cloud_hint_mentions_signin(self):
        ok, msg = oc.check(self.host, "thing:cloud")
        self.assertFalse(ok)
        self.assertIn("ollama signin", msg)

    def test_check_server_down(self):
        ok, msg = oc.check("http://127.0.0.1:1", "m")
        self.assertFalse(ok)
        self.assertIn("ollama serve", msg)


class PromptTests(unittest.TestCase):
    def test_read_inputs_and_escape(self):
        with tempfile.TemporaryDirectory() as t:
            root = Path(t) / "p"
            root.mkdir()
            (root / "a.py").write_text("A\n")
            (Path(t) / "secret.txt").write_text("S\n")
            self.assertEqual(oc.read_inputs(root, ["a.py"]), {"a.py": "A\n"})
            with self.assertRaises(oc.WorkerError):
                oc.read_inputs(root, ["../secret.txt"])
            with self.assertRaises(oc.WorkerError):
                oc.read_inputs(root, ["missing.py"])

    def test_build_messages_contains_everything(self):
        msgs = oc.build_messages("do the thing", {"a.py": "A\n"}, ["out.py"])
        self.assertEqual(msgs[0]["role"], "system")
        self.assertIn("### FILE:", msgs[0]["content"])
        user = msgs[1]["content"]
        self.assertIn("do the thing", user)
        self.assertIn('<file path="a.py">', user)
        self.assertIn("A\n", user)
        self.assertIn("- out.py", user)


class MainTests(unittest.TestCase):
    def setUp(self):
        self.srv, self.host = start_fake()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        FakeOllama.reply = "Here:\n### FILE: out.py\n```python\nprint('hi')\n```\n"
        FakeOllama.models = ["present:latest"]
        FakeOllama.status = 200
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.root = Path(self._t.name)
        self.cfg = write_cfg(self._t.name, {
            "default_profile": "p",
            "profiles": {"p": {"model": "present", "host": self.host},
                         "empty": {"model": "", "host": self.host}},
            "timeout_sec": 5})

    def run_main(self, argv, brief="make out.py print hi"):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = oc.main(["--config", str(self.cfg), "--root", str(self.root)] + argv,
                           stdin=io.StringIO(brief))
        return code, out.getvalue(), err.getvalue()

    def test_happy_path_writes_and_prints_diff(self):
        code, out, _ = self.run_main(["--write", "out.py"])
        self.assertEqual(code, 0)
        self.assertEqual((self.root / "out.py").read_text(), "print('hi')\n")
        self.assertIn("+print('hi')", out)

    def test_reference_file_is_sent(self):
        (self.root / "ref.py").write_text("REFCONTENT\n")
        self.run_main(["--write", "out.py", "--read", "ref.py"])
        self.assertIn("REFCONTENT", FakeOllama.last_request["messages"][1]["content"])

    def test_no_write_flag_is_error(self):
        code, _, err = self.run_main([])
        self.assertEqual(code, 1)
        self.assertIn("--write", err)

    def test_prose_only_reply_fails_and_writes_nothing(self):
        FakeOllama.reply = "I cannot help with that."
        code, _, err = self.run_main(["--write", "out.py"])
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "out.py").exists())

    def test_only_disallowed_blocks_fails(self):
        FakeOllama.reply = "### FILE: other.py\n```\nx\n```\n"
        code, out, err = self.run_main(["--write", "out.py"])
        self.assertEqual(code, 1)
        self.assertFalse((self.root / "other.py").exists())
        self.assertIn("other.py", out + err)

    def test_empty_brief_is_error(self):
        code, _, err = self.run_main(["--write", "out.py"], brief="  \n")
        self.assertEqual(code, 1)

    def test_empty_model_profile_message(self):
        code, _, err = self.run_main(["--profile", "empty", "--write", "out.py"])
        self.assertEqual(code, 1)
        self.assertIn("ollama list", err)

    def test_check_ok_and_fail(self):
        code, out, _ = self.run_main(["--check"])
        self.assertEqual(code, 0)
        FakeOllama.models = []
        code, out, _ = self.run_main(["--check"])
        self.assertEqual(code, 1)
        self.assertIn("ollama pull present", out)


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.srv, self.host = start_fake()
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        FakeOllama.models = ["present:latest"]
        FakeOllama.status = 200
        FakeOllama.reply = "### FILE: a.py\n```\nx\n```\n"
        FakeOllama.last_auth = None

    def test_chat_and_check_send_bearer(self):
        oc.chat(self.host, "present", [], 5, 10, api_key="SECRET")
        self.assertEqual(FakeOllama.last_auth, "Bearer SECRET")
        FakeOllama.last_auth = None
        oc.check(self.host, "present", api_key="SECRET")
        self.assertEqual(FakeOllama.last_auth, "Bearer SECRET")

    def test_no_key_no_header(self):
        oc.chat(self.host, "present", [], 5, 10)
        self.assertIsNone(FakeOllama.last_auth)

    def test_resolve_profile_exposes_api_key_env(self):
        cfg = {"profiles": {"c": {"model": "m", "api_key_env": "MY_KEY"}}}
        self.assertEqual(oc.resolve_profile(cfg, "c")[1]["api_key_env"], "MY_KEY")

    def test_get_api_key(self):
        self.assertIsNone(oc.get_api_key({"model": "m", "host": "h"}, {}))
        self.assertEqual(oc.get_api_key({"api_key_env": "K"}, {"K": "v"}), "v")
        with self.assertRaisesRegex(oc.WorkerError, "K"):
            oc.get_api_key({"api_key_env": "K"}, {})

    def test_main_uses_env_key(self):
        import os
        with tempfile.TemporaryDirectory() as t:
            cfg = write_cfg(t, {"profiles": {"c": {"model": "present", "host": self.host,
                                                   "api_key_env": "OC_TEST_KEY"}}})
            os.environ["OC_TEST_KEY"] = "ENVSECRET"
            self.addCleanup(os.environ.pop, "OC_TEST_KEY", None)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                code = oc.main(["--config", str(cfg), "--root", t, "--write", "a.py"],
                               stdin=io.StringIO("brief"))
            self.assertEqual(code, 0)
            self.assertEqual(FakeOllama.last_auth, "Bearer ENVSECRET")
            self.assertNotIn("ENVSECRET", out.getvalue())


class EnvFileTests(unittest.TestCase):
    def write_env(self, text):
        t = tempfile.TemporaryDirectory()
        self.addCleanup(t.cleanup)
        p = Path(t.name) / ".env"
        p.write_text(text)
        return str(p)

    def prof(self, path):
        return {"api_key_env": "K", "env_file": path}

    def test_formats(self):
        for line in ['K=plain', 'export K=plain', 'K="plain"', "K='plain'", '  K = plain  ']:
            path = self.write_env("# c\nOTHER=zzz\n" + line + "\nMORE=1\n")
            self.assertEqual(oc.get_api_key(self.prof(path), {}), "plain", line)

    def test_environ_takes_precedence(self):
        path = self.write_env("K=fromfile\n")
        self.assertEqual(oc.get_api_key(self.prof(path), {"K": "fromenv"}), "fromenv")

    def test_missing_file(self):
        with self.assertRaisesRegex(oc.WorkerError, "env_file"):
            oc.get_api_key(self.prof("/nonexistent/.env"), {})

    def test_var_absent_in_file(self):
        path = self.write_env("OTHER=1\n")
        with self.assertRaisesRegex(oc.WorkerError, "K"):
            oc.get_api_key(self.prof(path), {})

    def test_error_never_contains_secret(self):
        path = self.write_env("OTHER=topsecret\n")
        with self.assertRaises(oc.WorkerError) as cm:
            oc.get_api_key(self.prof(path), {})
        self.assertNotIn("topsecret", str(cm.exception))

    def test_tilde_expanded(self):
        import os
        home = tempfile.TemporaryDirectory()
        self.addCleanup(home.cleanup)
        (Path(home.name) / ".env").write_text("K=tilde\n")
        old = os.environ.get("HOME")
        os.environ["HOME"] = home.name
        self.addCleanup(lambda: os.environ.__setitem__("HOME", old))
        self.assertEqual(oc.get_api_key({"api_key_env": "K", "env_file": "~/.env"}, {}), "tilde")

    def test_resolve_profile_exposes_env_file(self):
        cfg = {"profiles": {"c": {"model": "m", "api_key_env": "K", "env_file": "~/.env"}}}
        self.assertEqual(oc.resolve_profile(cfg, "c")[1]["env_file"], "~/.env")


if __name__ == "__main__":
    unittest.main()
