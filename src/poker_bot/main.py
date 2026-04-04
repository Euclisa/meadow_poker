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
        help="Comma-separated player types in seat order, using only cli and llm.",
    )
    cli_parser.add_argument(
        "--max-hands",
        type=int,
        default=None,
        help="Maximum hands to play. Omit for unlimited (plays until one player remains).",
    )

    subparsers.add_parser("telegram", help="Run the Telegram bot")

    return parser


async def run_cli_mode(config: ProjectConfig, *, players_spec: str, max_hands: int) -> None:
    player_types = [item.strip().lower() for item in players_spec.split(",") if item.strip()]
    if len(player_types) < 2:
        raise ValueError("CLI mode requires at least 2 players.")
    if len(player_types) > config.game.max_players:
        raise ValueError("CLI player count cannot exceed game.max_players from the config file.")

    seats: list[SeatConfig] = []
    agents = {}
    llm_client: LLMGameClient | None = None
    llm_names = BotNameAllocator()
    _validate_cli_players(player_types, config.llm)
    logger.debug("Starting CLI mode with players=%s max_hands=%s", player_types, max_hands)
    for index, player_type in enumerate(player_types, start=1):
        seat_id = f"p{index}"
        if player_type == "cli":
            seats.append(SeatConfig(seat_id=seat_id, name=f"CLI {index}"))
            agents[seat_id] = CLIPlayerAgent(seat_id)
        elif player_type == "llm":
            seats.append(SeatConfig(seat_id=seat_id, name=llm_names.allocate()))
            if llm_client is None:
                llm_client = LLMGameClient(
                    model=config.llm.model,
                    api_key=config.llm.api_key,
                    base_url=config.llm.base_url,
                    timeout=config.llm.timeout,
                    max_output_tokens=config.llm.max_output_tokens,
                )
            agents[seat_id] = LLMPlayerAgent(seat_id, client=llm_client)
        else:
            raise ValueError(f"Unsupported player type: {player_type}")

    engine = PokerEngine.create_table(
        TableConfig(
            small_blind=config.game.small_blind,
            big_blind=config.game.big_blind,
            starting_stack=config.game.starting_stack,
            max_players=config.game.max_players,
        ),
        seats,
    )
    orchestrator = GameOrchestrator(engine, agents)
    await orchestrator.run(max_hands=max_hands)


async def run_telegram_mode(config: ProjectConfig) -> None:
    logger.debug("Starting Telegram mode with username=%s", config.telegram.bot_username)
    app = TelegramApp(
        TelegramAppConfig(
            bot_token=config.telegram.bot_token,
            bot_username=config.telegram.bot_username,
            llm_model=config.llm.model,
            llm_api_key=config.llm.api_key,
            llm_base_url=config.llm.base_url,
            llm_timeout=config.llm.timeout,
            llm_max_output_tokens=config.llm.max_output_tokens,
            small_blind=config.game.small_blind,
            big_blind=config.game.big_blind,
            starting_stack=config.game.starting_stack,
            max_players=config.game.max_players,
            max_hands_per_table=config.telegram.max_hands_per_table,
        )
    )
    await app.run_polling()


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
        asyncio.run(run_cli_mode(config, players_spec=args.players, max_hands=args.max_hands))
        return
    if args.mode == "telegram":
        asyncio.run(run_telegram_mode(config))
        return
    parser.error(f"Unknown mode: {args.mode}")


def _validate_cli_players(player_types: list[str], llm: LLMSettings) -> None:
    unsupported = [player_type for player_type in player_types if player_type not in {"cli", "llm"}]
    if unsupported:
        raise ValueError(f"Unsupported CLI player types: {', '.join(sorted(set(unsupported)))}")
    if "llm" in player_types and (llm.model is None or llm.api_key is None):
        raise ValueError("llm.model and llm.api_key must be set in the config file when CLI uses llm seats.")
