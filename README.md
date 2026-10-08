# opus-ollama

A Claude Code skill (`/opus-ollama`) where **Opus plans and reviews** and a **tool-less Ollama model writes the code** (local or Ollama cloud), modelled on `opusplan` but with Sonnet replaced by a cheap open model.

> **Honest status:** it works, but on small and medium tasks it was **not cheaper** than `opusplan`. See [Results](#results) before you expect savings.

## How it works

- **Opus (your session)** explores, designs, splits the work, writes briefs, runs tests and reviews diffs. None of that is delegated.
- **The worker** is a pure text-in/text-out call to Ollama's `/api/chat`: no tools, no agent loop, no repo access. It sees only the brief and the files you pass with `--read`.
- **`ollama-code.sh`** inlines the files, calls the model, parses `### FILE:` blocks from the reply, writes only paths on the `--write` allowlist (and inside the project root; traversal, absolute paths and symlink escapes are rejected), then prints a diff for Opus to review.

## Install

Requirements: Python 3 (stdlib only), Ollama (local server and/or an Ollama cloud API key), Claude Code.

```bash
git clone <this repo> && cd opus-ollama
ln -s "$PWD/opus-ollama" ~/.claude/skills/opus-ollama
```

Edit `~/.claude/skills/opus-ollama/config.json`:

```json
{
  "default_profile": "cloud",
  "profiles": {
    "local": { "model": "", "host": "http://localhost:11434" },
    "cloud": {
      "model": "gpt-oss:20b",
      "host": "https://ollama.com",
      "api_key_env": "OLLAMA_API_KEY",
      "env_file": "~/.env"
    }
  },
  "timeout_sec": 300,
  "max_output_tokens": 16000
}
```

- `local`: set `model` to something from `ollama list` (nothing is hardcoded to any machine).
- `cloud`: calls `https://ollama.com` directly. The API key is read from the environment variable named in `api_key_env`, or, if unset, from that single variable in `env_file`. The key is never printed and never stored in the config.
- Cloud model availability depends on your Ollama plan (a paid-only model returns HTTP 402).

Check it: `~/.claude/skills/opus-ollama/scripts/ollama-code.sh --check [--profile NAME]`. Then run `/opus-ollama` in Claude Code.

## Usage

`SKILL.md` tells Claude how to use it. The script itself:

```bash
ollama-code.sh [--profile NAME] --read ctx.py --write out.py --write test_out.py <<'BRIEF'
...task brief...
BRIEF
```

Flags: `--profile`, `--config`, `--root`, `--read` (repeatable), `--write` (repeatable, allowlist), `--check`.

## Tests

```bash
python3 -m unittest discover -s opus-ollama/tests -v
```

49 tests, including a fake Ollama server, path-traversal/symlink cases and API-key handling.

## Results

Same medium task twice (a JSON-backed task-queue CLI: storage, CLI, tests; about 3K tokens of generated code), orchestrated by Claude Code:

| | Run 1 | Run 2 |
|---|---|---|
| API calls | 6 | 6 |
| Cache-read tokens | 1.11M | 1.18M |
| Output tokens | 4.1K | 5.9K |
| Priced as Sonnet 5.5 | $0.28 | $0.32 |
| Priced as Opus 5.5 | $0.34 | $0.40 |

Both runs needed one worker retry plus a small manual fix. Delegation added turns, and every turn re-reads the whole context, which outweighed the output tokens saved. It should pay off only for bulk generation (tens of thousands of generated tokens in few turns). Caveats: both runs were orchestrated by Sonnet 5.5 (so the Opus row is a re-pricing), and the session context was large (about 185K tokens per call), which inflates cache-read cost.

## Layout

```
opus-ollama/            # the skill (symlink this into ~/.claude/skills/)
  SKILL.md
  config.json
  scripts/ollama-code.sh, ollama_code.py
  tests/test_ollama_code.py
docs/superpowers/specs/ # design spec
```

## License

MIT
