#!/usr/bin/env python3
"""Tool-less code-generation bridge to Ollama: brief in, files out."""
import argparse
import difflib
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / "config.json"
DEFAULT_HOST = "http://localhost:11434"


class WorkerError(Exception):
    pass


def load_config(path):
    try:
        cfg = json.loads(Path(path).read_text())
    except FileNotFoundError:
        raise WorkerError(f"config not found: {path}")
    except json.JSONDecodeError as e:
        raise WorkerError(f"invalid config JSON in {path}: {e}")
    if not isinstance(cfg.get("profiles"), dict) or not cfg["profiles"]:
        raise WorkerError(f"config {path} has no profiles")
    cfg.setdefault("timeout_sec", 300)
    cfg.setdefault("max_output_tokens", 8192)
    return cfg


def resolve_profile(cfg, name=None):
    name = name or cfg.get("default_profile") or next(iter(cfg["profiles"]))
    prof = cfg["profiles"].get(name)
    if prof is None:
        raise WorkerError(f"unknown profile '{name}'; available: {', '.join(cfg['profiles'])}")
    model = (prof.get("model") or "").strip()
    if not model:
        raise WorkerError(
            f"profile '{name}' has no model set; run `ollama list` and put a model name "
            f"in the profile in config.json")
    host = (prof.get("host") or DEFAULT_HOST).rstrip("/")
    resolved = {"model": model, "host": host}
    for field in ("api_key_env", "env_file"):
        if prof.get(field):
            resolved[field] = prof[field]
    return name, resolved


def _read_env_var(path, var):
    """Return var's value from a dotenv-style file, reading only that variable."""
    path = os.path.expanduser(path)
    try:
        lines = Path(path).read_text().splitlines()
    except OSError as e:
        raise WorkerError(f"cannot read env_file {path}: {e.strerror or e}")
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        if sep and name.strip() == var:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value or None
    return None


def get_api_key(prof, environ):
    var = prof.get("api_key_env")
    if not var:
        return None
    key = environ.get(var)
    if not key and prof.get("env_file"):
        key = _read_env_var(prof["env_file"], var)
    if not key:
        where = f" (also looked in env_file {prof['env_file']})" if prof.get("env_file") else ""
        raise WorkerError(f"profile needs an API key but {var} is not set{where}")
    return key


def _auth_headers(api_key):
    return {"Authorization": f"Bearer {api_key}"} if api_key else {}


HEADER = "### FILE:"


def _finish_block(path, lines, blocks):
    start = next((i for i, l in enumerate(lines) if l.startswith("```")), None)
    if start is None:
        return
    body = lines[start + 1:]
    closers = [i for i, l in enumerate(body)
               if len(l.strip()) >= 3 and set(l.strip()) == {"`"}]
    if closers:
        body = body[:closers[-1]]
    while body and not body[-1].strip():
        body.pop()
    blocks.append((path, "\n".join(body) + "\n" if body else ""))


def parse_blocks(reply):
    blocks, path, lines = [], None, []
    for line in reply.splitlines():
        if line.startswith(HEADER):
            if path is not None:
                _finish_block(path, lines, blocks)
            path, lines = line[len(HEADER):].strip(), []
        elif path is not None:
            lines.append(line)
    if path is not None:
        _finish_block(path, lines, blocks)
    return blocks


def normalize(rel):
    return os.path.normpath(rel)


def safe_target(root, rel, allowed):
    rel = normalize(rel)
    if rel not in {normalize(a) for a in allowed}:
        return None
    if os.path.isabs(rel):
        return None
    root = Path(root).resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return None
    return target


def apply_blocks(blocks, root, allowed):
    written, rejected = [], []
    for rel, content in blocks:
        target = safe_target(root, rel, allowed)
        if target is None:
            rejected.append((rel, "not in --write list or outside project root"))
            continue
        old = target.read_text() if target.exists() else ""
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        written.append((normalize(rel), old, content))
    return written, rejected


def format_report(written, rejected):
    out = [f"Wrote {len(written)} file(s): " + (", ".join(w[0] for w in written) or "none")]
    for rel, reason in rejected:
        out.append(f"REJECTED {rel}: {reason}")
    for rel, old, new in written:
        diff = difflib.unified_diff(old.splitlines(), new.splitlines(),
                                    fromfile=f"a/{rel}", tofile=f"b/{rel}", lineterm="")
        out.append("\n".join(diff))
    return "\n".join(out)


SYSTEM_PROMPT = """You are a code generator. You have no tools and cannot run anything.
Do exactly what the task says using only the task text and the provided files.
Output ONLY the requested files, each in this exact format and nothing else:

### FILE: relative/path.ext
```
<complete file contents>
```

Rules: output the COMPLETE contents of every file you write, never diffs or
placeholders. Only write the files listed under "Files you must write". No
explanations, no commentary, no extra files."""


def read_inputs(root, rels):
    root = Path(root).resolve()
    files = {}
    for rel in rels:
        p = (root / rel).resolve()
        try:
            p.relative_to(root)
        except ValueError:
            raise WorkerError(f"--read path outside project root: {rel}")
        if not p.is_file():
            raise WorkerError(f"--read file not found: {rel}")
        files[normalize(rel)] = p.read_text()
    return files


def build_messages(brief, files, write_paths):
    parts = ["## Task", brief.strip(), "", "## Files you must write (output each in full)"]
    parts += [f"- {p}" for p in write_paths]
    if files:
        parts += ["", "## Reference files"]
        for rel, content in files.items():
            parts.append(f'<file path="{rel}">\n{content}</file>')
    return [{"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": "\n".join(parts)}]


def chat(host, model, messages, timeout, max_tokens, api_key=None):
    body = json.dumps({"model": model, "messages": messages, "stream": False,
                       "options": {"num_predict": max_tokens}}).encode()
    req = urllib.request.Request(host + "/api/chat", data=body,
                                 headers={"Content-Type": "application/json", **_auth_headers(api_key)})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
    except urllib.error.HTTPError as e:
        raise WorkerError(f"Ollama returned HTTP {e.code}: "
                          f"{e.read().decode(errors='replace')[:300]}")
    except (urllib.error.URLError, OSError) as e:
        raise WorkerError(f"cannot reach Ollama at {host}: {e}")
    text = (data.get("message") or {}).get("content", "")
    if not text.strip():
        raise WorkerError("empty reply from model")
    return text


def check(host, model, api_key=None):
    try:
        req = urllib.request.Request(host + "/api/tags", headers=_auth_headers(api_key))
        with urllib.request.urlopen(req, timeout=5) as r:
            tags = json.load(r)
    except urllib.error.HTTPError as e:
        return False, f"Ollama at {host} returned HTTP {e.code} (check the API key / signin)."
    except (urllib.error.URLError, OSError) as e:
        return False, f"Ollama not reachable at {host} ({e}). Start it with `ollama serve`."
    names = {m.get("name") for m in tags.get("models", []) if m.get("name")}
    if names & {model, model if ":" in model else model + ":latest"}:
        return True, f"ok: {model} available at {host}"
    fix = f"`ollama pull {model}`"
    if "cloud" in model:
        fix = f"`ollama signin`, then `ollama pull {model}`"
    installed = ", ".join(sorted(names)) or "none"
    return False, f"model '{model}' not found (installed: {installed}). Fix: {fix}"


def main(argv=None, stdin=None):
    ap = argparse.ArgumentParser(description="Tool-less Ollama code generator")
    ap.add_argument("brief_file", nargs="?", help="task brief file (default: stdin)")
    ap.add_argument("--profile")
    ap.add_argument("--config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--root", default=os.getcwd())
    ap.add_argument("--read", action="append", default=[])
    ap.add_argument("--write", action="append", default=[])
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args(argv)
    try:
        cfg = load_config(args.config)
        name, prof = resolve_profile(cfg, args.profile)
        if args.check:
            ok, msg = check(prof["host"], prof["model"], get_api_key(prof, os.environ))
            print(f"[{name}] {msg}")
            return 0 if ok else 1
        if not args.write:
            raise WorkerError("at least one --write PATH is required")
        src = Path(args.brief_file).read_text() if args.brief_file else (stdin or sys.stdin).read()
        if not src.strip():
            raise WorkerError("empty task brief")
        files = read_inputs(args.root, args.read)
        messages = build_messages(src, files, args.write)
        reply = chat(prof["host"], prof["model"], messages,
                     cfg["timeout_sec"], cfg["max_output_tokens"],
                     api_key=get_api_key(prof, os.environ))
        written, rejected = apply_blocks(parse_blocks(reply), args.root, set(args.write))
        report = format_report(written, rejected)
        if not written:
            print(report)
            raise WorkerError("model reply contained no allowed file blocks; nothing written")
        print(report)
        return 0
    except WorkerError as e:
        print(f"ollama-code: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
