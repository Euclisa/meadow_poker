from __future__ import annotations

import argparse
import asyncio
import builtins
import logging

from poker_bot.backend.http import HttpBackendClient, create_backend_http_app
from poker_bot.backend.models import ActorRef, ManagedTableConfig
from poker_bot.backend.serialization import game_event_from_dict, snapshot_pending_decision, snapshot_player_view, snapshot_public_table_view
from poker_bot.backend.service import LocalBackendClient, LocalTableBackendService
from poker_bot.config import DEFAULT_CONFIG_PATH, LLMSettings, ProjectConfig, load_project_config
from poker_bot.logging_utils import configure_logging
from poker_bot.naming import BotNameAllocator
from poker_bot.players.llm import LLMGameClient
from poker_bot.players.rendering import render_cli_events, render_cli_public_events, render_cli_standings, render_cli_status, render_cli_turn_prompt
from poker_bot.telegram_app.app import TelegramApp, TelegramAppConfig
from poker_bot.types import ActionType, PlayerAction, PlayerUpdate, PlayerUpdateType

logger = logging.getLogger(__name__)

_ACTION_SHORTCUTS: dict[str, str] = {
    "f": "fold",
    "c": "call",
    "k": "check",
    "b": "bet",
    "r": "raise",
}

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="poker-bot")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH), help="Path to the TOML config file.")
    parser.add_argument("--debug", action="store_true", help="Enable verbose debug logging including private game and LLM data.")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    cli_parser = subparsers.add_parser("cli", help="Run a local CLI table")
    cli_parser.add_argument("--players", required=True, help="Comma-separated seat list in order: use 'bot' for an LLM seat, anything else as a human player name.")
    cli_parser.add_argument("--max-hands", type=int, default=None, help="Maximum hands to play. Omit for unlimited (plays until one player remains).")
    cli_parser.add_argument("--big-blind", type=int, default=100, help="Big blind amount. Defaults to 100.")
    cli_parser.add_argument("--small-blind", type=int, default=None, help="Small blind amount. Defaults to half of --big-blind.")
    cli_parser.add_argument("--starting-stack", type=int, default=None, help="Starting stack size. Defaults to 20 times --big-blind.")
    cli_parser.add_argument("--ante", type=int, default=0, help="Per-player ante amount. Defaults to 0.")
    cli_parser.add_argument("--turn-timeout", type=int, default=None, help="Optional per-turn timeout in seconds. Omit to disable.")

    subparsers.add_parser("telegram", help="Run the Telegram bot")
    subparsers.add_parser("web", help="Run the web lobby and table UI")
    subparsers.add_parser("backend", help="Run the standalone backend server")

    return parser


async def run_cli_mode(
    config: ProjectConfig,
    *,
    players_spec: str,
    max_hands: int | None,
    big_blind: int = 100,
    small_blind: int | None = None,
    starting_stack: int | None = None,
    ante: int = 0,
    turn_timeout: int | None = None,
) -> None:
    player_entries = [item.strip() for item in players_spec.split(",") if item.strip()]
    if len(player_entries) < 2:
        raise ValueError("CLI mode requires at least 2 players.")
    if len(player_entries) > config.game.max_players:
        raise ValueError("CLI player count cannot exceed game.max_players from the config file.")
    resolved_small_blind = max(1, big_blind // 2) if small_blind is None else small_blind
    resolved_starting_stack = big_blind * 20 if starting_stack is None else starting_stack
    if ante < 0:
        raise ValueError("CLI ante must be non-negative.")
    if turn_timeout is not None and turn_timeout <= 0:
        raise ValueError("CLI turn timeout must be positive when set.")
    _validate_cli_players(player_entries, config.llm)

    backend = _build_backend_client(config)
    llm_seat_count = sum(1 for entry in player_entries if entry.casefold() == "bot")
    human_names = [entry for entry in player_entries if entry.casefold() != "bot"]
    creator_actor = ActorRef(
        transport="cli",
        external_id="observer" if not human_names else "p1",
        display_name=human_names[0] if human_names else "CLI observer",
    )
    create_result = await backend.create_table(
        creator_actor,
        ManagedTableConfig(
            total_seats=len(player_entries),
            llm_seat_count=llm_seat_count,
            small_blind=resolved_small_blind,
            big_blind=big_blind,
            ante=ante,
            starting_stack=resolved_starting_stack,
            turn_timeout_seconds=turn_timeout,
            max_hands_per_table=max_hands,
            max_players=config.game.max_players,
            human_transport="cli",
            human_seat_prefix="p",
        ),
    )
    table_id = create_result["table_id"]
    actor_entries: list[tuple[ActorRef, str]] = []
    if human_names:
        actor_entries.append((creator_actor, create_result["viewer_token"]))
    human_index = 1
    for entry in player_entries[1:]:
        if entry.casefold() == "bot":
            continue
        human_index += 1
        actor = ActorRef(transport="cli", external_id=f"p{human_index}", display_name=entry)
        join_result = await backend.join_table(actor, table_id)
        actor_entries.append((actor, join_result["viewer_token"]))
    await backend.start_table(creator_actor, table_id, create_result["viewer_token"])

    watcher_token = create_result["viewer_token"]
    human_tokens = {actor.external_id: token for actor, token in actor_entries}
    snapshot = await backend.get_table_snapshot(table_id, watcher_token)
    version = int(snapshot.get("version", 0))
    while True:
        public_table_payload = snapshot.get("public_table")
        player_view_payload = snapshot.get("player_view")
        if snapshot.get("status") == "completed":
            public_view = snapshot.get("public_table")
            if public_view is not None:
                print(render_cli_standings(snapshot_public_table_view(public_view)))
            return
        acting_seat_id = snapshot.get("public_table", {}).get("acting_seat_id")
        if acting_seat_id is not None and acting_seat_id in human_tokens:
            seat_snapshot = await backend.get_table_snapshot(table_id, human_tokens[acting_seat_id])
            pending_payload = seat_snapshot.get("pending_decision")
            if pending_payload is not None and seat_snapshot.get("player_view") is not None and seat_snapshot.get("public_table") is not None:
                decision = snapshot_pending_decision(pending_payload, seat_snapshot)
                print(render_cli_status(decision.player_view))
                print(render_cli_turn_prompt(decision))
                action = await _prompt_cli_action(decision)
                await backend.submit_action(table_id, human_tokens[acting_seat_id], action)
        new_payload = await backend.wait_for_table_version(table_id, watcher_token, version, 15_000)
        snapshot = new_payload["snapshot"]
        version = int(snapshot.get("version", version))
        public_table_payload = snapshot.get("public_table") or public_table_payload
        player_view_payload = snapshot.get("player_view") or player_view_payload
        if public_table_payload is not None and new_payload.get("new_events"):
            public_view = snapshot_public_table_view(public_table_payload)
            if player_view_payload is not None:
                update = PlayerUpdate(
                    update_type=_infer_cli_update_type(new_payload["new_events"], snapshot),
                    events=tuple(game_event_from_dict(item) for item in new_payload["new_events"]),
                    public_table_view=public_view,
                    player_view=snapshot_player_view(player_view_payload, public_table_payload),
                    acting_seat_id=public_table_payload.get("acting_seat_id"),
                    is_your_turn=snapshot.get("pending_decision") is not None,
                )
                event_text = render_cli_events(update)
                if event_text:
                    print(event_text)
                if update.update_type == PlayerUpdateType.TABLE_COMPLETED:
                    print(render_cli_standings(update.public_table_view))
                    return
            else:
                event_text = render_cli_public_events(
                    tuple(game_event_from_dict(item) for item in new_payload["new_events"]),
                    public_view,
                )
                if event_text:
                    print(event_text)


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
        ),
        backend=_build_backend_client(config),
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
        ),
        backend=_build_backend_client(config, showdown_delay_seconds=config.web.showdown_delay_seconds),
    )
    await app.run()


async def run_backend_mode(config: ProjectConfig) -> None:
    try:
        from aiohttp import web
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("The aiohttp package is required for backend mode.") from exc
    service = _build_local_backend_service(config, showdown_delay_seconds=config.backend.showdown_delay_seconds)
    app = create_backend_http_app(service)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host=config.backend.host, port=config.backend.port)
    await site.start()
    logger.info("Backend server available at http://%s:%s", config.backend.host, config.backend.port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    config = load_project_config(args.config)
    configure_logging(mode=args.mode, debug=args.debug, config_level=config.game.log_level)
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
                ante=args.ante,
                turn_timeout=args.turn_timeout,
            )
        )
        return
    if args.mode == "telegram":
        asyncio.run(run_telegram_mode(config))
        return
    if args.mode == "web":
        asyncio.run(run_web_mode(config))
        return
    if args.mode == "backend":
        asyncio.run(run_backend_mode(config))
        return
    parser.error(f"Unknown mode: {args.mode}")


def _build_backend_client(config: ProjectConfig, *, showdown_delay_seconds: float | None = None) -> Any:
    if config.backend.mode.value == "remote":
        return HttpBackendClient(config.backend.gateway_url or "")
    return LocalBackendClient(
        _build_local_backend_service(
            config,
            showdown_delay_seconds=config.backend.showdown_delay_seconds if showdown_delay_seconds is None else showdown_delay_seconds,
        )
    )


def _build_local_backend_service(config: ProjectConfig, *, showdown_delay_seconds: float) -> LocalTableBackendService:
    return LocalTableBackendService(
        llm_client_factory=lambda: LLMGameClient(settings=config.llm),
        coach_client_factory=(lambda: LLMGameClient(settings=config.coach)) if config.coach.enabled else None,
        llm_name_allocator=BotNameAllocator(),
        llm_recent_hand_count=config.llm.recent_hand_count,
        llm_thought_logging=config.llm.thought_logging,
        coach_enabled=config.coach.enabled,
        coach_recent_hand_count=config.coach.recent_hand_count,
        showdown_delay_seconds=showdown_delay_seconds,
    )


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


def _infer_cli_update_type(new_events: list[dict[str, object]], snapshot: dict[str, Any]) -> PlayerUpdateType:
    if any(event["event_type"] == "table_completed" for event in new_events):
        return PlayerUpdateType.TABLE_COMPLETED
    if snapshot.get("pending_decision") is not None:
        return PlayerUpdateType.TURN_STARTED
    return PlayerUpdateType.STATE_CHANGED


async def _prompt_cli_action(decision: Any) -> PlayerAction:
    legal = {action.action_type.value: action for action in decision.legal_actions}
    while True:
        raw = (await _read_cli_text("> ")).strip().lower()
        resolved = _ACTION_SHORTCUTS.get(raw, raw.split()[0] if raw else "")
        if resolved not in legal:
            choices = ", ".join(f"[{action[0]}]{action[1:]}" for action in legal)
            print(f"  Illegal choice. Options: {choices}")
            continue
        selected = legal[resolved]
        if selected.action_type in {ActionType.BET, ActionType.RAISE}:
            amount = await _read_cli_amount(selected.min_amount or 0, selected.max_amount or 0)
            if amount is None:
                continue
            return PlayerAction(selected.action_type, amount=amount)
        return PlayerAction(selected.action_type)


async def _read_cli_text(prompt: str) -> str:
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, builtins.input, prompt)


async def _read_cli_amount(lo: int, hi: int) -> int | None:
    if lo == hi:
        print(f"  All-in: {lo}")
        return lo
    raw = (await _read_cli_text(f"  Amount ({lo}-{hi}): ")).strip()
    try:
        amount = int(raw)
    except ValueError:
        print("  Enter a number.")
        return None
    if amount < lo:
        print(f"  Minimum is {lo}.")
        return None
    if amount > hi:
        print(f"  Maximum is {hi}.")
        return None
    return amount
