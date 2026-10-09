# Security Policy

## Supported versions

Only the latest commit on `main` is supported.

## Reporting a vulnerability

Please **do not open a public issue** for a security problem. Use GitHub's private reporting instead: go to the repository's **Security** tab and choose **Report a vulnerability**. Include what you found, how to reproduce it, and the impact.

This is a small personal project, so responses are best-effort. I'll acknowledge reports as soon as I can and credit reporters who want it.

## What is in scope

`opus-ollama` runs a script that sends a task brief and chosen files to an Ollama model and writes the model's reply to disk. Things worth reporting:

- **Path-guard bypass:** any way for model output to write a file outside the `--write` allowlist or outside the project root (path traversal, absolute paths, symlink escapes).
- **Secret exposure:** any way the API key (read from the `api_key_env` variable or the single variable in `env_file`) could be printed, logged, written to disk or sent anywhere except the configured Ollama host.
- **Reading outside the root:** any way `--read` could include a file outside the project root.

## Things to know as a user

- **Cloud profiles send data off your machine.** The brief and every `--read` file go to the configured Ollama host (for example `https://ollama.com`). Do not pass secrets or private code you are not willing to share with that provider.
- **The worker has no tools.** It cannot run commands or read your repository. It only returns text, which the script writes to the paths you allowlisted.
- **Review diffs.** The script prints a diff of everything it writes. Read it before you rely on the output.
