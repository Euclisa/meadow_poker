# Meadow

Meadow is a Python project for running no-limit Texas Hold'em with a deterministic game engine, a transport-agnostic orchestrator, and interchangeable player agents for CLI humans, Telegram humans, browser humans, and LLM seats.

## Installation

1. Create and activate a virtual environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create the local config file from the template:

```bash
cp config/config.toml.example config/config.toml
```

4. Fill in the secrets and settings inside `config/config.toml`.

## Configuration

Shared runtime configuration lives in `config/config.toml`. Per-table blinds, ante, and stack can be set from CLI flags or during Telegram table creation.

- `[game]` controls shared table-size limits and logging.
- `[llm]` configures the OpenAI-compatible backend used by LLM seats.
  - `max_output_tokens` is optional. If omitted, no output-token cap is sent to the provider.
  - `recent_hand_count` controls how many completed hand summaries trigger an internal reflection-note update for each LLM seat.
  - `thought_logging` controls LLM thought logging: `off`, `notes`, or `full`.
  - Provider-specific subsections such as `[llm.openrouter]` are resolved from `llm.base_url`, so only the settings for the active gateway are applied.
  - `[llm.openrouter].sort` accepts `price`, `throughput`, or `latency` and is sent as OpenRouter `provider.sort`.
- `[coach]` configures the optional per-table LLM coach used by web and Telegram players on their turn.
  - `enabled` turns the feature on.
  - `recent_hand_count` controls how many completed public hand summaries trigger a rolling public table-note update.
  - Transport fields mirror `[llm]`, including provider-specific subsections such as `[coach.openrouter]`.
- `[backend]` configures the shared table backend that owns waiting tables, running tables, orchestrator state, LLM seats, coach access, and replay history.
  - `mode = "local"` runs the backend in-process inside the selected app.
  - `mode = "remote"` makes CLI, Telegram, and web talk to a standalone backend server at `gateway_url`.
  - `host` and `port` are used by `python3 -m meadow backend`.
  - `showdown_delay_seconds` controls the backend-owned showdown pacing in local mode and for the standalone backend server.
- `[telegram]` configures the Telegram bot interaction layer.
- `[web]` configures the browser interaction layer.
  - `host` and `port` control where the HTTP server listens.
  - `max_hands_per_table` is optional and mirrors the Telegram table cap behavior.
  - `showdown_delay_seconds` is the local web default when it spins up an in-process backend.

LLM seat display names are drawn from [names.txt](src/meadow/data/names.txt) and get a `_bot` suffix, for example `Nova_bot`. Telegram human seats use the Telegram display name passed by the bot runtime.

The committed template is `config/config.toml.example`. The real `config/config.toml` is ignored by git so secrets stay local to your worktree.

## Execution

Run the CLI table against the configured backend:

```bash
PYTHONPATH=src python3 -m meadow --config config/config.toml cli --players Alice,bot,Bob --max-hands 1 --big-blind 100 --ante 10 --starting-stack 2000
```

Run the Telegram bot:

```bash
PYTHONPATH=src python3 -m meadow --config config/config.toml telegram
```

Telegram table creation prompts for seat counts, blinds, ante, and starting stack, with `Default` shortcuts for the standard values.

Run the web lobby and table UI:

```bash
PYTHONPATH=src python3 -m meadow --config config/config.toml web
```

Then open `http://127.0.0.1:8080` in your browser, unless you changed `[web].host` or `[web].port`.

Run the standalone backend server:

```bash
PYTHONPATH=src python3 -m meadow --config config/config.toml backend
```

In `backend.mode = "remote"`, the CLI, Telegram bot, and web app all talk to `backend.gateway_url` instead of creating their own in-process backend.

## Deployment

The simplest supported production layout is one Linux server running Meadow as two `systemd` services:

- `meadow-backend` runs the standalone backend on `127.0.0.1:8090`.
- `meadow-telegram` runs the Telegram bot and talks to the backend over HTTP.

This repo includes the deploy assets in `deploy/`:

- `deploy/deploy.sh`
- `deploy/systemd/meadow-backend.service`
- `deploy/systemd/meadow-telegram.service`

### Deployed Config Shape

For the Telegram bot to use the standalone backend, set the shared config to remote mode:

```toml
[backend]
mode = "remote"
gateway_url = "http://127.0.0.1:8090"
host = "127.0.0.1"
port = 8090
showdown_delay_seconds = 5.0
```

The backend service reads the same `config/config.toml`, but only `host`, `port`, and the shared service settings matter for `python3 -m meadow backend`.

### One-Time Server Setup

The committed unit files assume this layout:

- service user: `meadow`
- app checkout: `/home/meadow/app`
- virtualenv: `/home/meadow/app/.venv`
- config: `/home/meadow/app/config/config.toml`

Example setup:

```bash
sudo useradd --create-home --shell /bin/bash meadow
sudo -u meadow git clone <your-repo-url> /home/meadow/app
cd /home/meadow/app
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
cp config/config.toml.example config/config.toml
```

Fill in `config/config.toml`, especially:

- Telegram bot token and username
- LLM and coach credentials if you use them
- `[backend].mode = "remote"`
- `[backend].gateway_url = "http://127.0.0.1:8090"`

Install the services:

```bash
sudo cp deploy/systemd/meadow-backend.service /etc/systemd/system/
sudo cp deploy/systemd/meadow-telegram.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now meadow-backend meadow-telegram
```

### Routine Deploy

After pushing new code to the server, deploy with:

```bash
cd /home/meadow/app
./deploy/deploy.sh
```

The script pulls the latest code with `git pull --ff-only`, ensures the virtualenv and dependencies are up to date, runs a focused test suite, and restarts both services.

### Operations

Check service status:

```bash
sudo systemctl status meadow-backend meadow-telegram
```

Restart manually:

```bash
sudo systemctl restart meadow-backend meadow-telegram
```

Tail logs through journald:

```bash
sudo journalctl -u meadow-backend -u meadow-telegram -f
```

### Troubleshooting

- Invalid config: run `PYTHONPATH=src .venv/bin/python -m meadow --config config/config.toml backend` manually to surface TOML and validation errors.
- Missing Python dependencies: rerun `.venv/bin/pip install -r requirements.txt`.
- Telegram cannot reach the backend: confirm `meadow-backend` is running and `gateway_url` is set to `http://127.0.0.1:8090`.
- Service restart loop: inspect `sudo journalctl -u meadow-backend -u meadow-telegram -n 200`.

## Web UI

The web runtime is a vanilla HTML/CSS/JS frontend served by an `aiohttp` app. It is a thin browser adapter over the shared backend contract: the backend owns waiting tables, running tables, completed tables, orchestrator state, human action mailboxes, LLM seats, coach requests, and replay history.

- Create a table with a display name, total seat count, and LLM seat count.
- Share the table link or code with other browser players.
- Rejoin your seat after refresh using a browser-stored viewer token.
- Waiting tables are public in the lobby; running and completed tables require a valid seat token.
- Running-table leave is intentionally unsupported in v1. Refresh and reconnect are handled by the saved seat token instead.

## CLI Entry Point

The `cli` entry point requires the local table layout as explicit command-line arguments and submits those choices through the same backend contract used by the web and Telegram apps.

- `--players` is a comma-separated seat list such as `Alice,bot,Bob`.
- `bot` creates an LLM-controlled seat using the shared `[llm]` section from the config file.
- Any other token, including `cli`, creates a terminal-controlled human seat and uses that token as the display name.
- Human names must be unique within the table.
- `--max-hands` controls how many hands the local run will play before exiting.
- `--big-blind` defaults to `100`.
- `--small-blind` defaults to half of `--big-blind`.
- `--ante` defaults to `0`.
- `--starting-stack` defaults to `20` big blinds.

This keeps `config/config.toml` focused on shared services and backend connectivity, while the CLI command itself explicitly describes the table you want to run.
