# opus-ollama — Design Spec

Date: 2026-10-08

## Purpose

`/opus-ollama` is a Claude Code skill that behaves like the built-in `opusplan`
(Opus plans and orchestrates) but replaces the Sonnet execution tier with a cheap
Ollama model (local or Ollama cloud) that does only well-specified coding.

## Requirements

1. Opus (the current session) does all exploration, research, design, debugging,
   test running and review. None of it is ever delegated.
2. The Ollama worker is a pure code generator: text in, code out. No tools, no
   agent loop, no file discovery, no commands. It only knows what the brief and
   the inlined files tell it.
3. Both local and Ollama-cloud models are supported and configurable from the
   skill, via named profiles in one config file.
4. Opus must not re-emit code the worker produced. It reviews diffs only.
5. Sonnet is not used.

## Non-goals

- No `claude -p` headless sessions, no MCP server, no agent loop around Ollama.
- No automatic test running or self-repair by the worker.
- No change to Claude Code's model selection (a skill cannot add a model alias).

## Components

```
~/.claude/skills/opus-ollama/
  SKILL.md              # workflow rules for Opus
  config.json           # profiles and limits
  scripts/ollama-code.sh
```

### config.json

```json
{
  "default_profile": "local",
  "profiles": {
    "local": { "model": "<chosen at build>", "host": "http://localhost:11434" },
    "cloud": { "model": "<chosen at build>-cloud", "host": "http://localhost:11434" }
  },
  "timeout_sec": 300,
  "max_output_tokens": 8192
}
```

- Any number of profiles may be added.
- Cloud models are served through the same local Ollama server (after a one-time
  `ollama signin`), so the script has a single code path. This is to be verified
  at build time; if cloud needs a different host or auth, it becomes a profile
  field.
- Concrete model names are chosen at build time after checking the machine's RAM
  and what Ollama currently offers. None are pulled today (`ollama list` is empty).

### scripts/ollama-code.sh

Inputs: a task brief (file or stdin), `--profile <name>` (optional),
`--read <path>` (repeatable), `--write <path>` (repeatable), `--check`.

Flow:
1. Load config, resolve profile.
2. Build the prompt: system prompt demanding code only, in the exact format
   below; user prompt = brief + contents of every `--read` file (the script reads
   them, not the model).
3. `POST {host}/api/chat` (non-streaming) with the timeout and output token limit.
4. Parse the reply into `### FILE: <path>` blocks, each followed by one fenced
   code block.
5. Write a block only if its path is in the `--write` allowlist and resolves
   inside the project directory. Any other path is rejected and reported.
6. Print a summary (files written, files rejected) and a unified diff.

Output format required from the model:

````
### FILE: relative/path.ext
```
<complete file contents>
```
````

`--check`: verifies the server is reachable, the profile's model is pulled and,
for cloud profiles, that signin is done. On failure prints the exact fix command
(`ollama pull ...` / `ollama signin`) and exits non-zero.

Errors: unreachable server, timeout, empty or unparseable reply, or no allowed
blocks all exit non-zero with a clear message and write nothing.

### SKILL.md

Instructs Opus to:
1. Work plan-first like `opusplan`: explore, design and decompose with its own
   tools; never delegate those.
2. Write one self-contained brief per coding task: goal, exact interfaces,
   constraints, acceptance check, plus the read and write file lists.
3. Run `ollama-code.sh`, then review the diff.
4. Accept, or retry once with a sharper brief, or do the task itself if the retry
   fails.
5. Run tests and verification itself.
6. Choose the profile: default from config, `/opus-ollama <profile>` overrides
   for the session, and Opus may pick another profile per task (e.g. cloud for
   larger tasks).
7. Run `ollama-code.sh --check` at skill start and stop with the fix command if
   the check fails.

## Trade-offs

- With no tools the worker cannot run tests or self-correct; briefs must be
  precise. Opus covers verification.
- Whole-file output is simple and safe to parse but costs more tokens on large
  files; tasks should be sized to small files or new files.

## Testing

1. `--check` passes for each profile and fails with a useful message when the
   model is not pulled or the server is down.
2. Parser unit tests on canned replies: valid, extra prose, missing block,
   disallowed path, path traversal (`../`).
3. End-to-end dry run on a small real task, confirming exploration and review
   stay with Opus and only generation goes to Ollama.

## Open items resolved at build time

- Concrete local and cloud model names.
- Whether cloud models need anything beyond `ollama signin`.
