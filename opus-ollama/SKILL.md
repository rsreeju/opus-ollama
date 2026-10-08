---
name: opus-ollama
description: Opus plans and orchestrates like opusplan, but delegates well-specified coding tasks to a cheap tool-less Ollama model (local or cloud) via a script. Use when the user invokes /opus-ollama or asks to use Ollama for the grunt coding work.
---

# opus-ollama

You (Opus) are the orchestrator. A cheap Ollama model is a **code-only typist**: it
has no tools, cannot read the repo, cannot run anything. It sees only the brief and
the files you pass it.

## Never delegate
Exploration, research, reading the codebase, design, planning, debugging, running
tests, and reviewing. Do all of that yourself, exactly as in plan-first mode.

## Setup (once per session)
Optional argument: a profile name (`/opus-ollama cloud`). Otherwise use the
config's `default_profile`.

1. Run `~/.claude/skills/opus-ollama/scripts/ollama-code.sh --check [--profile NAME]`.
2. If it fails, show the user the message and fix command, and stop. If the profile
   has no model set, run `ollama list`, show the installed models, and ask the user
   which to put in `~/.claude/skills/opus-ollama/config.json`. Never choose a model
   for them. Cloud models need `ollama signin` once.

## Workflow
1. Plan first: explore and design yourself; split the work into small coding tasks.
2. Only delegate tasks that are fully specified and self-contained: new files,
   mechanical edits, boilerplate, implementing a defined function or interface.
   Do ambiguous, cross-cutting, or tricky tasks yourself.
3. For each delegated task write a brief containing: the goal, exact interfaces and
   signatures, constraints, edge cases, and the acceptance check. Assume the model
   knows nothing beyond the brief and the files you pass.
4. Run (brief on stdin):

   ```bash
   ~/.claude/skills/opus-ollama/scripts/ollama-code.sh [--profile NAME] \
     --read path/to/context.py --write path/to/out.py <<'BRIEF'
   ...the brief...
   BRIEF
   ```

   `--read` files are inlined for the model. The script writes only `--write` paths
   inside the project and prints a diff. The model returns whole files, so size tasks
   to small or new files.
5. Review the printed diff. Do NOT re-emit code the model got right.
6. Run the tests / verification yourself.
7. If wrong: retry once with a sharper brief that names the specific defect. If it
   fails again, write the code yourself.
8. You may use a different profile per task (e.g. the cloud profile for larger tasks).

## Batching (this is what makes it cheaper)
Every brief you write is Opus output and every diff you read is Opus input, so delegation
only pays when the worker returns a lot of code per turn of yours.
- **Bundle:** one call per coherent module or feature, with several `--write` files
  (implementation and its tests together). Aim for 1-4 calls per feature, not one per file.
- **Specify everything up front** in a single brief: all file paths, exact signatures,
  data shapes, error behaviour and the test cases, so the worker never needs a follow-up.
- **Don't delegate small stuff:** if the code is under ~40 lines or a one-line edit, write
  it yourself; the brief would cost more than the code.
- **Keep briefs tight:** bullets and signatures, no prose, no restating what the worker can
  infer from the interfaces.
- **Few turns:** after a delegation run the tests once, read the diff once, fix or retry
  once. Don't re-read whole files the worker wrote; the printed diff is enough.

## Rules
- Non-zero exit means nothing was written; read the message and fix the cause.
- Never pass secrets in briefs or `--read` files: their contents go to the model
  (and off-machine for cloud profiles).
