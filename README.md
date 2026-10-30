# Verbot

Verbot is a local-first command-line AI assistant. It uses Ollama for chat,
remembers conversation context for the current session, and can launch desktop
applications by their menu names.

## Requirements and setup

- Python 3.10 or newer
- [Ollama](https://ollama.com/download) for AI chat
- A desktop session for launching graphical apps

There are no third-party Python dependencies.

Download the default model once:

```sh
ollama pull qwen2.5:1.5b
```

Then start Verbot:

```sh
python3 verbot.py       # Linux
py verbot.py            # Windows
```

Verbot checks for a local Ollama service and starts it in the background when
needed. If another Ollama service is already running, Verbot reuses it. Set
`VERBOT_MODEL` to choose another installed model, or `VERBOT_OLLAMA_URL` to
use a different Ollama-compatible `/api/chat` or `/api/generate` endpoint.

## Chat and commands

Verbot keeps up to the last 3 exchanges as context in the active session.
`/clear` clears that conversation context. Durable preferences are
saved separately in the per-user config directory when requested.

- `/help` shows the available commands.
- `/launch <app>` opens an app; natural requests such as “open Firefox” work too.
- `/close <app>` gracefully asks the OS to close a supported app.
- `/token status`, `/token set`, and `/token clear` manage an optional API token.
- `/exit` closes Verbot.

Linux app discovery checks common executable names and desktop menu entries.
Windows checks common install locations and Start Menu shortcuts. Verbot only
closes a small allowlist of apps, using exact process names.

Telegram requests such as “open Telegram and write to Alex” open Telegram,
but do not send a message. You must choose the recipient and send it yourself.

## Token storage

`VERBOT_API_TOKEN` takes precedence over the saved token. Otherwise, `/token set`
stores it in `%APPDATA%\verbot\token` on Windows or
`$XDG_CONFIG_HOME/verbot/token` on Linux (falling back to `~/.config`). On
Linux the file is restricted to the current user. The token is sent as an HTTP
Bearer token for authenticated Ollama-compatible endpoints; local Ollama does
not need a token. Token values are never displayed.

## Other files

`learn.py` is a small, independent logistic-regression learning demo. It does
not train or change the Ollama model used by Verbot.

The `.gitignore` excludes local caches, tokens, and memory files. GitHub
Actions runs the standard-library checks on Ubuntu and Windows for Python 3.10
and 3.13. Choose a license before publishing if you want others to reuse the
project.
# Verbot
