# Poker Bot

Poker Bot is a Python project for running no-limit Texas Hold'em with a deterministic game engine, a transport-agnostic orchestrator, and interchangeable player agents for CLI humans, Telegram humans, browser humans, and LLM seats.

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

All runtime configuration lives in `config/config.toml`.

- `[game]` controls blinds, stack size, and table size limits.
- `[llm]` configures the OpenAI-compatible backend used by LLM seats.
  - `max_output_tokens` is optional. If omitted, no output-token cap is sent to the provider.
  - `recent_hand_count` controls how many completed hand summaries trigger an internal reflection-note update for each LLM seat.
  - `log_thoughts` writes completed-hand summaries and reflection-note updates to the app log at `INFO`.
- `[telegram]` configures the Telegram bot runtime.
- `[web]` configures the browser lobby and table runtime.
  - `host` and `port` control where the HTTP server listens.
  - `max_hands_per_table` is optional and mirrors the Telegram table cap behavior.

LLM seat display names are drawn from [names.txt](/home/canary/Documents/Code/hse/poker_bot/src/poker_bot/data/names.txt) and get a `_bot` suffix, for example `Nova_bot`. Telegram human seats use the Telegram display name passed by the bot runtime.

The committed template is `config/config.toml.example`. The real `config/config.toml` is ignored by git so secrets stay local to your worktree.

## Execution

Run the CLI table:

```bash
PYTHONPATH=src python3 -m poker_bot --config config/config.toml cli --players Alice,bot,Bob --max-hands 1
```

Run the Telegram bot:

```bash
PYTHONPATH=src python3 -m poker_bot --config config/config.toml telegram
```

Run the web lobby and table UI:

```bash
PYTHONPATH=src python3 -m poker_bot --config config/config.toml web
```

Then open `http://127.0.0.1:8080` in your browser, unless you changed `[web].host` or `[web].port`.

## Web UI

The web runtime is a vanilla HTML/CSS/JS frontend served by an `aiohttp` backend. It keeps the existing poker engine and orchestrator intact and adds a browser-specific lobby/session layer.

- Create a table with a display name, total seat count, and LLM seat count.
- Share the table link or code with other browser players.
- Rejoin your seat after refresh using a browser-stored seat token.
- Waiting tables are public in the lobby; running and completed tables require a valid seat token.
- Running-table leave is intentionally unsupported in v1. Refresh/reconnect is handled by the saved seat token instead.

## CLI Entry Point

The `cli` entry point now requires the local table layout as explicit command-line arguments.

- `--players` is a comma-separated seat list such as `Alice,bot,Bob`.
- `bot` creates an LLM-controlled seat using the shared `[llm]` section from the config file.
- Any other token, including `cli`, creates a terminal-controlled human seat and uses that token as the display name.
- Human names must be unique within the table.
- `--max-hands` controls how many hands the local run will play before exiting.

This keeps `config/config.toml` focused on shared services and game defaults, while the CLI command itself explicitly describes the local table you want to run.
