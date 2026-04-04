# Poker Bot

Poker Bot is a Python project for running no-limit Texas Hold'em with a deterministic game engine, a transport-agnostic orchestrator, and interchangeable player agents for CLI humans, Telegram humans, and LLM seats.

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
- `[telegram]` configures the Telegram bot runtime.

The committed template is `config/config.toml.example`. The real `config/config.toml` is ignored by git so secrets stay local to your worktree.

## Execution

Run the CLI table:

```bash
PYTHONPATH=src python -m poker_bot --config config/config.toml cli --players cli,llm --max-hands 1
```

Run the Telegram bot:

```bash
PYTHONPATH=src python -m poker_bot --config config/config.toml telegram
```

## CLI Entry Point

The `cli` entry point now requires the local table layout as explicit command-line arguments.

- `--players` is a comma-separated seat list such as `cli,llm,cli`.
- `cli` creates a terminal-controlled human seat.
- `llm` creates an LLM-controlled seat using the shared `[llm]` section from the config file.
- `--max-hands` controls how many hands the local run will play before exiting.

This keeps `config/config.toml` focused on shared services and game defaults, while the CLI command itself explicitly describes the local table you want to run.
