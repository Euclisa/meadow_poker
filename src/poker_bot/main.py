from __future__ import annotations

import argparse
import asyncio
import logging

from poker_bot.config import DEFAULT_CONFIG_PATH, LLMSettings, ProjectConfig, load_project_config
from poker_bot.logging_utils import configure_logging
from poker_bot.naming import BotNameAllocator
from poker_bot.orchestrator import GameOrchestrator
from poker_bot.players.cli import CLIPlayerAgent
from poker_bot.players.llm import LLMGameClient, LLMPlayerAgent
from poker_bot.poker.engine import PokerEngine
from poker_bot.table_runner import run_table
from poker_bot.telegram_app.app import TelegramApp, TelegramAppConfig
from poker_bot.types import SeatConfig, TableConfig

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="poker-bot")
    parser.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help="Path to the TOML config file.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable verbose debug logging including private game and LLM data.",
    )
    subparsers = parser.add_subparsers(dest="mode", required=True)

    cli_parser = subparsers.add_parser("cli", help="Run a local CLI table")
    cli_parser.add_argument(
        "--players",
        required=True,
        help="Comma-separated seat list in order: use 'bot' for an LLM seat, anything else as a human player name.",
    )
    cli_parser.add_argument(
        "--max-hands",
        type=int,
        default=None,
        help="Maximum hands to play. Omit for unlimited (plays until one player remains).",
    )
    cli_parser.add_argument(
        "--big-blind",
        type=int,
        default=100,
        help="Big blind amount. Defaults to 100.",
    )
    cli_parser.add_argument(
        "--small-blind",
        type=int,
        default=None,
        help="Small blind amount. Defaults to half of --big-blind.",
    )
    cli_parser.add_argument(
        "--starting-stack",
        type=int,
        default=None,
        help="Starting stack size. Defaults to 20 times --big-blind.",
    )

    subparsers.add_parser("telegram", help="Run the Telegram bot")
    subparsers.add_parser("web", help="Run the web lobby and table UI")

    return parser


async def run_cli_mode(
    config: ProjectConfig,
    *,
    players_spec: str,
    max_hands: int | None,
    big_blind: int = 100,
    small_blind: int | None = None,
    starting_stack: int | None = None,
) -> None:
    player_entries = [item.strip() for item in players_spec.split(",") if item.strip()]
    if len(player_entries) < 2:
        raise ValueError("CLI mode requires at least 2 players.")
    if len(player_entries) > config.game.max_players:
        raise ValueError("CLI player count cannot exceed game.max_players from the config file.")
    resolved_small_blind = max(1, big_blind // 2) if small_blind is None else small_blind
    resolved_starting_stack = big_blind * 20 if starting_stack is None else starting_stack

    seats: list[SeatConfig] = []
    agents = {}
    llm_client: LLMGameClient | None = None
    llm_names = BotNameAllocator()
    _validate_cli_players(player_entries, config.llm)
    logger.debug("Starting CLI mode with players=%s max_hands=%s", player_entries, max_hands)
    for index, player_entry in enumerate(player_entries, start=1):
        seat_id = f"p{index}"
        if player_entry.casefold() == "bot":
            seats.append(SeatConfig(seat_id=seat_id, name=llm_names.allocate()))
            if llm_client is None:
                llm_client = LLMGameClient(
                    settings=config.llm,
                )
            agents[seat_id] = LLMPlayerAgent(
                seat_id,
                client=llm_client,
                recent_hand_count=config.llm.recent_hand_count,
                thought_logging=config.llm.thought_logging,
            )
        else:
            seats.append(SeatConfig(seat_id=seat_id, name=player_entry))
            agents[seat_id] = CLIPlayerAgent(seat_id)

    engine = PokerEngine.create_table(
        TableConfig(
            small_blind=resolved_small_blind,
            big_blind=big_blind,
            starting_stack=resolved_starting_stack,
            max_players=config.game.max_players,
        ),
        seats,
    )
    orchestrator = GameOrchestrator(engine, agents)
    await run_table(orchestrator, max_hands=max_hands)


async def run_telegram_mode(config: ProjectConfig) -> None:
    logger.debug("Starting Telegram mode with username=%s", config.telegram.bot_username)
    app = TelegramApp(
        TelegramAppConfig(
            bot_token=config.telegram.bot_token,
            bot_username=config.telegram.bot_username,
            llm=config.llm,
            coach=config.coach,
            max_players=config.game.max_players,
            max_hands_per_table=config.telegram.max_hands_per_table,
        )
    )
    await app.run_polling()


async def run_web_mode(config: ProjectConfig) -> None:
    from poker_bot.web_app.app import WebApp, WebAppConfig

    logger.debug("Starting web mode on %s:%s", config.web.host, config.web.port)
    app = WebApp(
        WebAppConfig(
            host=config.web.host,
            port=config.web.port,
            llm=config.llm,
            coach=config.coach,
            max_players=config.game.max_players,
            max_hands_per_table=config.web.max_hands_per_table,
            showdown_delay_seconds=config.web.showdown_delay_seconds,
        )
    )
    await app.run()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config = load_project_config(args.config)
    configure_logging(
        mode=args.mode,
        debug=args.debug,
        config_level=config.game.log_level,
    )
    logger.debug("Loaded config from %s", args.config)
    if args.mode == "cli":
        asyncio.run(
            run_cli_mode(
                config,
                players_spec=args.players,
                max_hands=args.max_hands,
                big_blind=args.big_blind,
                small_blind=args.small_blind,
                starting_stack=args.starting_stack,
            )
        )
        return
    if args.mode == "telegram":
        asyncio.run(run_telegram_mode(config))
        return
    if args.mode == "web":
        asyncio.run(run_web_mode(config))
        return
    parser.error(f"Unknown mode: {args.mode}")


def _validate_cli_players(player_entries: list[str], llm: LLMSettings) -> None:
    human_name_keys: dict[str, str] = {}
    duplicate_names: list[str] = []
    has_bot = False

    for player_entry in player_entries:
        if player_entry.casefold() == "bot":
            has_bot = True
            continue
        normalized_name = player_entry.casefold()
        if normalized_name in human_name_keys:
            duplicate_names.append(player_entry)
            continue
        human_name_keys[normalized_name] = player_entry

    if duplicate_names:
        duplicates = ", ".join(sorted(set(duplicate_names), key=str.casefold))
        raise ValueError(f"Duplicate CLI user names are not allowed: {duplicates}")
    if has_bot and (llm.model is None or llm.api_key is None):
        raise ValueError("llm.model and llm.api_key must be set in the config file when CLI uses bot seats.")
