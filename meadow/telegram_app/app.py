from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import html
import json
import logging
from typing import Any

from meadow.backend.models import ActorRef, ManagedTableConfig
from meadow.backend.serialization import game_event_from_dict, snapshot_pending_decision, snapshot_player_view, snapshot_public_table_view
from meadow.backend.service import BackendError, LocalBackendClient, LocalTableBackendService
from meadow.config import CoachSettings, LLMSettings
from meadow.naming import BotNameAllocator
from meadow.llm_bot import LLMGameClient
from meadow.rendering.telegram import (
    render_telegram_status_panel,
    render_telegram_turn_prompt,
    render_telegram_update_messages,
)
from meadow.types import ActionType, PlayerAction, PlayerUpdate, PlayerUpdateType, TelegramTableState

logger = logging.getLogger(__name__)
_INVALID_TIMEOUT = object()


@dataclass(frozen=True, slots=True)
class TelegramAppConfig:
    bot_token: str | None = None
    bot_username: str | None = None
    small_blind: int = 50
    big_blind: int = 100
    ante: int = 0
    starting_stack: int = 2_000
    max_players: int = 6
    llm: LLMSettings = field(default_factory=LLMSettings)
    coach: CoachSettings = field(default_factory=CoachSettings)
    max_hands_per_table: int | None = None


@dataclass(slots=True)
class _CreateTableFlowState:
    chat_id: int
    total_seats: int | None = None
    llm_seat_count: int | None = None
    big_blind: int | None = None
    small_blind: int | None = None
    ante: int | None = None
    starting_stack: int | None = None
    turn_timeout_seconds: int | None = None
    turn_timeout_configured: bool = False


@dataclass(slots=True)
class _PendingAmountState:
    table_id: str
    viewer_token: str
    action_type: ActionType
    snapshot: dict[str, Any]


@dataclass(slots=True)
class _WatcherState:
    user_id: int
    chat_id: int
    table_id: str
    viewer_token: str
    display_name: str
    last_status_text: str | None = None
    last_prompt_signature: str | None = None
    seen_recent_event_ids: set[str] = field(default_factory=set)
    task: asyncio.Task[Any] | None = None


class TelegramApp:
    def __init__(
        self,
        config: TelegramAppConfig,
        *,
        send_message: Any | None = None,
        llm_client_factory: Any | None = None,
        coach_client_factory: Any | None = None,
        llm_name_allocator: BotNameAllocator | None = None,
        bot: Any | None = None,
        backend: Any | None = None,
    ) -> None:
        self.config = config
        self._bot = bot
        self._send_message_callback = send_message
        self._llm_client_factory = llm_client_factory or self._default_llm_client_factory
        self._coach_client_factory = coach_client_factory or self._default_coach_client_factory
        self._llm_name_allocator = llm_name_allocator
        self._create_flows: dict[int, _CreateTableFlowState] = {}
        self._pending_amounts: dict[int, _PendingAmountState] = {}
        self._watchers: dict[tuple[int, str], _WatcherState] = {}
        self._coach_pending_user_ids: set[int] = set()
        self.backend = backend or self._build_local_backend()

    async def handle_start_command(
        self,
        *,
        user_id: int,
        chat_id: int,
        display_name: str,
        payload: str | None = None,
    ) -> None:
        logger.debug("Handling /start user_id=%s chat_id=%s payload=%s", user_id, chat_id, payload)
        if payload and payload.startswith("join_"):
            await self.handle_join_command(
                user_id=user_id,
                chat_id=chat_id,
                display_name=display_name,
                table_id=payload.removeprefix("join_"),
            )
            return
        await self._send_message(
            chat_id,
            "Welcome to Meadow.\nUse /create_table to start a table or /join <table_id> to join one.",
            await self._build_lobby_keyboard(user_id=user_id, chat_id=chat_id),
        )

    async def handle_help_command(self, *, chat_id: int) -> None:
        await self._send_message(
            chat_id,
            "\n".join(
                [
                    "/start - intro",
                    "/create_table - begin guided table creation",
                    "/join <table_id> - join a waiting table",
                    "/my_table - show your current table",
                    "/start_game - creator starts a full waiting table",
                    "/leave_table - leave a waiting table",
                    "/cancel_table - creator cancels a waiting table",
                    "/coach <question> - ask the table coach on your turn",
                    "/help - show this help",
                ]
            ),
            await self._build_lobby_keyboard(),
        )

    async def handle_create_table_command(self, *, user_id: int, chat_id: int) -> None:
        logger.debug("Handling /create_table user_id=%s chat_id=%s", user_id, chat_id)
        self._sync_local_backend_settings()
        tables = await self._actor_tables(user_id=user_id, chat_id=chat_id, display_name=str(user_id))
        if any(item["status"] in {"waiting", "running"} for item in tables["tables"]):
            await self._send_message(chat_id, "You are already assigned to a table.")
            return
        self._create_flows[user_id] = _CreateTableFlowState(chat_id=chat_id)
        await self._send_message(
            chat_id,
            "Enter total number of players for the table (2-6).",
            self._build_create_flow_keyboard(),
        )

    async def handle_join_command(
        self,
        *,
        user_id: int,
        chat_id: int,
        display_name: str,
        table_id: str,
    ) -> None:
        logger.debug("Handling /join user_id=%s chat_id=%s table_id=%s", user_id, chat_id, table_id)
        try:
            result = await self.backend.join_table(self._actor(user_id, chat_id, display_name), table_id)
        except BackendError as exc:
            await self._send_message(chat_id, exc.message, await self._build_lobby_keyboard(user_id=user_id, chat_id=chat_id))
            return
        snapshot = result["snapshot"]
        await self._notify_waiting_participants(
            result.get("participants", snapshot.get("participants", ())),
            self._format_waiting_table_update(
                snapshot,
                f"{display_name} joined table {snapshot['table_id']}.",
            ),
            emphasize_start=self._is_waiting_table_full(snapshot),
        )

    async def handle_my_table_command(self, *, user_id: int, chat_id: int) -> None:
        entry = await self._primary_actor_table(user_id=user_id, chat_id=chat_id, display_name=str(user_id))
        if entry is None:
            await self._send_message(chat_id, "You are not assigned to any table.")
            return
        snapshot = await self.backend.get_table_snapshot(entry["table_id"], entry["viewer_token"])
        await self._send_message(chat_id, self._format_status(snapshot), await self._build_lobby_keyboard(user_id=user_id, chat_id=chat_id))

    async def handle_start_game_command(self, *, user_id: int, chat_id: int) -> None:
        logger.debug("Handling /start_game user_id=%s chat_id=%s", user_id, chat_id)
        self._sync_local_backend_settings()
        actor = self._actor(user_id, chat_id, str(user_id))
        entry = await self._primary_actor_table(user_id=user_id, chat_id=chat_id, display_name=str(user_id))
        if entry is None:
            await self._send_message(chat_id, "You are not assigned to any table.", await self._build_lobby_keyboard(user_id=user_id, chat_id=chat_id))
            return
        try:
            result = await self.backend.start_table(actor, entry["table_id"], entry["viewer_token"])
        except BackendError as exc:
            await self._send_message(chat_id, exc.message, await self._build_lobby_keyboard(user_id=user_id, chat_id=chat_id))
            return
        snapshot = result["snapshot"]
        participants = result.get("participants", snapshot.get("participants", ()))
        await self._notify_waiting_participants(participants, self._format_started_table_message(snapshot))
        await self._ensure_watchers(snapshot, participants=participants)

    async def handle_coach_command(self, *, user_id: int, chat_id: int, question: str) -> None:
        entry = await self._running_actor_table(user_id=user_id, chat_id=chat_id, display_name=str(user_id))
        if entry is None:
            await self._send_message(chat_id, "Coach tips are only available while your table is running.")
            return
        if not question.strip():
            await self._send_message(chat_id, "Usage: /coach <question>")
            return
        if user_id in self._coach_pending_user_ids:
            await self._send_message(chat_id, "Coach is already thinking for your seat.")
            return
        try:
            self._coach_pending_user_ids.add(user_id)
            await self._send_message(chat_id, "Coach is thinking...")
            result = await self.backend.request_coach(entry["table_id"], entry["viewer_token"], question)
        except BackendError as exc:
            await self._send_message(chat_id, exc.message)
            return
        finally:
            self._coach_pending_user_ids.discard(user_id)
        await self._send_message(chat_id, result["reply"])

    async def handle_leave_table_command(self, *, user_id: int, chat_id: int) -> None:
        logger.debug("Handling /leave_table user_id=%s chat_id=%s", user_id, chat_id)
        actor = self._actor(user_id, chat_id, str(user_id))
        entry = await self._primary_actor_table(user_id=user_id, chat_id=chat_id, display_name=str(user_id))
        if entry is None:
            await self._send_message(chat_id, "You are not assigned to any table.", await self._build_lobby_keyboard(user_id=user_id, chat_id=chat_id))
            return
        if entry["status"] == TelegramTableState.RUNNING.value:
            await self._send_message(chat_id, "Leaving a running table is not supported in v1.", await self._build_lobby_keyboard(user_id=user_id, chat_id=chat_id))
            return
        snapshot = await self.backend.get_table_snapshot(entry["table_id"], entry["viewer_token"])
        if snapshot["controls"]["is_creator"]:
            await self.handle_cancel_table_command(user_id=user_id, chat_id=chat_id)
            return
        try:
            result = await self.backend.leave_table(actor, entry["table_id"], entry["viewer_token"])
        except BackendError as exc:
            await self._send_message(chat_id, exc.message, await self._build_lobby_keyboard(user_id=user_id, chat_id=chat_id))
            return
        await self._send_message(chat_id, f"You left table {entry['table_id']}.", await self._build_lobby_keyboard(user_id=user_id, chat_id=chat_id))
        await self._notify_waiting_participants(
            result.get("participants", ()),
            self._format_waiting_table_update(
                result["snapshot"],
                f"{snapshot['controls']['viewer_name']} left table {entry['table_id']}.",
            ),
        )

    async def handle_cancel_table_command(self, *, user_id: int, chat_id: int) -> None:
        logger.debug("Handling /cancel_table user_id=%s chat_id=%s", user_id, chat_id)
        actor = self._actor(user_id, chat_id, str(user_id))
        entry = await self._primary_actor_table(user_id=user_id, chat_id=chat_id, display_name=str(user_id))
        if entry is None:
            await self._send_message(chat_id, "You are not assigned to any table.", await self._build_lobby_keyboard(user_id=user_id, chat_id=chat_id))
            return
        try:
            result = await self.backend.cancel_table(actor, entry["table_id"], entry["viewer_token"])
        except BackendError as exc:
            await self._send_message(chat_id, exc.message, await self._build_lobby_keyboard(user_id=user_id, chat_id=chat_id))
            return
        await self._notify_waiting_participants(result.get("participants", result["snapshot"].get("participants", ())), f"Table {entry['table_id']} was cancelled.")

    async def handle_callback_query(self, *, user_id: int, chat_id: int, data: str) -> bool:
        del user_id, chat_id, data
        return False

    async def handle_text_message(
        self,
        *,
        user_id: int,
        chat_id: int,
        display_name: str,
        text: str,
    ) -> None:
        logger.debug("Handling Telegram text user_id=%s chat_id=%s text=%s", user_id, chat_id, text)
        command = self._match_lobby_command(text)
        if command is not None and user_id not in self._create_flows:
            await command(user_id=user_id, chat_id=chat_id, display_name=display_name)
            return
        if user_id in self._create_flows and text.strip().lower() == "help":
            await self.handle_help_command(chat_id=chat_id)
            return
        if user_id in self._create_flows and text.strip().lower() == "cancel":
            self._create_flows.pop(user_id, None)
            await self._send_message(chat_id, "Table creation cancelled.", await self._build_lobby_keyboard(user_id=user_id, chat_id=chat_id))
            return
        if user_id in self._create_flows:
            await self._handle_create_flow_step(
                user_id=user_id,
                chat_id=chat_id,
                display_name=display_name,
                text=text,
            )
            return
        consumed = await self._route_action_text(user_id=user_id, chat_id=chat_id, display_name=display_name, text=text)
        if consumed:
            return
        await self._send_message(chat_id, "Unrecognized input. Use /help for commands.", await self._build_lobby_keyboard(user_id=user_id, chat_id=chat_id))

    async def run_polling(self) -> None:
        if self.config.bot_token is None:
            raise RuntimeError("bot_token is required to run Telegram polling")
        try:
            from aiogram import Bot, Dispatcher, F, Router
            from aiogram.filters import Command
            from aiogram.types import Message
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "The aiogram package is required for Telegram mode. Install meadow[telegram]."
            ) from exc

        bot = self._bot or Bot(self.config.bot_token)
        self._bot = bot
        router = Router()
        dispatcher = Dispatcher()
        dispatcher.include_router(router)

        @router.message(Command("start"))
        async def on_start(message: Message) -> None:
            payload = None
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) == 2:
                payload = parts[1]
            await self.handle_start_command(
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                display_name=message.from_user.full_name,
                payload=payload,
            )

        @router.message(Command("help"))
        async def on_help(message: Message) -> None:
            await self.handle_help_command(chat_id=message.chat.id)

        @router.message(Command("create_table"))
        async def on_create(message: Message) -> None:
            await self.handle_create_table_command(user_id=message.from_user.id, chat_id=message.chat.id)

        @router.message(Command("join"))
        async def on_join(message: Message) -> None:
            parts = (message.text or "").split(maxsplit=1)
            if len(parts) != 2:
                await self._send_message(message.chat.id, "Usage: /join <table_id>")
                return
            await self.handle_join_command(
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                display_name=message.from_user.full_name,
                table_id=parts[1].strip(),
            )

        @router.message(Command("my_table"))
        async def on_my_table(message: Message) -> None:
            await self.handle_my_table_command(user_id=message.from_user.id, chat_id=message.chat.id)

        @router.message(Command("start_game"))
        async def on_start_game(message: Message) -> None:
            await self.handle_start_game_command(user_id=message.from_user.id, chat_id=message.chat.id)

        @router.message(Command("leave_table"))
        async def on_leave_table(message: Message) -> None:
            await self.handle_leave_table_command(user_id=message.from_user.id, chat_id=message.chat.id)

        @router.message(Command("cancel_table"))
        async def on_cancel_table(message: Message) -> None:
            await self.handle_cancel_table_command(user_id=message.from_user.id, chat_id=message.chat.id)

        @router.message(Command("coach"))
        async def on_coach(message: Message) -> None:
            parts = (message.text or "").split(maxsplit=1)
            question = parts[1].strip() if len(parts) == 2 else ""
            await self.handle_coach_command(
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                question=question,
            )

        @router.message(F.text)
        async def on_text(message: Message) -> None:
            if message.text and message.text.startswith("/"):
                return
            await self.handle_text_message(
                user_id=message.from_user.id,
                chat_id=message.chat.id,
                display_name=message.from_user.full_name,
                text=message.text or "",
            )

        await dispatcher.start_polling(bot)

    async def _route_action_text(
        self,
        *,
        user_id: int,
        chat_id: int,
        display_name: str,
        text: str,
    ) -> bool:
        if user_id in self._coach_pending_user_ids:
            await self._send_message(chat_id, "Coach is still thinking. Please wait for the reply.")
            return True
        entry = await self._running_actor_table(user_id=user_id, chat_id=chat_id, display_name=display_name)
        if entry is None:
            return False
        snapshot = await self.backend.get_table_snapshot(entry["table_id"], entry["viewer_token"])
        pending = snapshot.get("pending_decision")
        if pending is None:
            return False
        amount_state = self._pending_amounts.get(user_id)
        if amount_state is not None:
            if text.strip().lower() in {"cancel", "back"}:
                self._pending_amounts.pop(user_id, None)
                await self._send_message(chat_id, "Amount entry cancelled.", self._build_action_keyboard(snapshot))
                return True
            return await self._submit_amount(user_id=user_id, chat_id=chat_id, amount_text=text, state=amount_state)
        action_name = self._normalize_action_text(text)
        if action_name is None:
            return False
        try:
            action_type = ActionType(action_name)
        except ValueError:
            return False
        legal_action = next((item for item in pending["legal_actions"] if item["action_type"] == action_type.value), None)
        if legal_action is None:
            await self._send_message(chat_id, "That action is not legal right now.", self._build_action_keyboard(snapshot))
            return True
        if action_type in {ActionType.BET, ActionType.RAISE}:
            self._pending_amounts[user_id] = _PendingAmountState(
                table_id=entry["table_id"],
                viewer_token=entry["viewer_token"],
                action_type=action_type,
                snapshot=snapshot,
            )
            await self._send_message(
                chat_id,
                f"Enter total amount for {action_type.value} ({legal_action['min_amount']}-{legal_action['max_amount']}).",
                None,
            )
            return True
        try:
            await self.backend.submit_action(entry["table_id"], entry["viewer_token"], PlayerAction(action_type=action_type))
        except BackendError as exc:
            await self._send_message(chat_id, exc.message)
        return True

    async def _submit_amount(
        self,
        *,
        user_id: int,
        chat_id: int,
        amount_text: str,
        state: _PendingAmountState,
    ) -> bool:
        pending = state.snapshot["pending_decision"]
        assert pending is not None
        legal_action = next((item for item in pending["legal_actions"] if item["action_type"] == state.action_type.value), None)
        if legal_action is None:
            self._pending_amounts.pop(user_id, None)
            await self._send_message(chat_id, "That action is no longer legal.", None)
            return True
        try:
            amount = int(amount_text.strip())
        except ValueError:
            await self._send_message(chat_id, "Enter a numeric total amount.", None)
            return True
        if legal_action["min_amount"] is not None and amount < legal_action["min_amount"]:
            await self._send_message(chat_id, f"Amount must be at least {legal_action['min_amount']}.", None)
            return True
        if legal_action["max_amount"] is not None and amount > legal_action["max_amount"]:
            await self._send_message(chat_id, f"Amount must be at most {legal_action['max_amount']}.", None)
            return True
        self._pending_amounts.pop(user_id, None)
        try:
            await self.backend.submit_action(
                state.table_id,
                state.viewer_token,
                PlayerAction(action_type=state.action_type, amount=amount),
            )
        except BackendError as exc:
            await self._send_message(chat_id, exc.message)
        return True

    async def _handle_create_flow_step(
        self,
        *,
        user_id: int,
        chat_id: int,
        display_name: str,
        text: str,
    ) -> None:
        logger.debug("Create flow step user_id=%s chat_id=%s text=%s", user_id, chat_id, text)
        self._sync_local_backend_settings()
        flow = self._create_flows[user_id]
        if flow.total_seats is None:
            total_seats = self._parse_int(text)
            if total_seats is None or not 2 <= total_seats <= self.config.max_players:
                await self._send_message(chat_id, f"Enter a valid player count between 2 and {self.config.max_players}.", self._build_create_flow_keyboard())
                return
            flow.total_seats = total_seats
            await self._send_message(chat_id, f"Enter number of LLM seats (0-{total_seats - 1}).", self._build_create_flow_keyboard())
            return
        if flow.llm_seat_count is None:
            llm_seats = self._parse_int(text)
            assert flow.total_seats is not None
            if llm_seats is None or not 0 <= llm_seats < flow.total_seats:
                await self._send_message(chat_id, f"Enter a valid LLM seat count between 0 and {flow.total_seats - 1}.", self._build_create_flow_keyboard())
                return
            flow.llm_seat_count = llm_seats
            await self._send_message(chat_id, f"Enter big blind. Type Default for {self.config.big_blind}.", self._build_create_flow_keyboard())
            return
        if flow.big_blind is None:
            big_blind = self._parse_int_or_default(text, default=self.config.big_blind)
            if big_blind is None or big_blind <= 0:
                await self._send_message(chat_id, "Enter a valid positive big blind, or type Default.", self._build_create_flow_keyboard())
                return
            flow.big_blind = big_blind
            await self._send_message(chat_id, f"Enter small blind. Type Default for {self._default_small_blind(big_blind)}.", self._build_create_flow_keyboard())
            return
        if flow.small_blind is None:
            assert flow.big_blind is not None
            small_blind = self._parse_int_or_default(text, default=self._default_small_blind(flow.big_blind))
            if small_blind is None or small_blind <= 0 or small_blind > flow.big_blind:
                await self._send_message(chat_id, f"Enter a valid small blind between 1 and {flow.big_blind}, or type Default.", self._build_create_flow_keyboard())
                return
            flow.small_blind = small_blind
            await self._send_message(chat_id, f"Enter ante per player. Type Off/Default for {self._format_ante(self.config.ante)}.", self._build_create_flow_keyboard())
            return
        if flow.ante is None:
            ante = self._parse_ante(text, default=self.config.ante)
            if ante is None:
                await self._send_message(chat_id, "Enter a valid non-negative ante, or type Off/Default.", self._build_create_flow_keyboard())
                return
            flow.ante = ante
            await self._send_message(chat_id, f"Enter starting stack. Type Default for {self._default_starting_stack(flow.big_blind)}.", self._build_create_flow_keyboard())
            return
        assert flow.total_seats is not None
        assert flow.llm_seat_count is not None
        assert flow.big_blind is not None
        assert flow.small_blind is not None
        assert flow.ante is not None
        if flow.starting_stack is None:
            starting_stack = self._parse_int_or_default(text, default=self._default_starting_stack(flow.big_blind))
            if starting_stack is None or starting_stack <= 0:
                await self._send_message(chat_id, "Enter a valid positive starting stack, or type Default.", self._build_create_flow_keyboard())
                return
            flow.starting_stack = starting_stack
            await self._send_message(chat_id, "Enter turn timer in seconds, or type Off/Default to disable it.", self._build_create_flow_keyboard())
            return
        if not flow.turn_timeout_configured:
            parsed_timeout = self._parse_turn_timeout(text)
            if parsed_timeout is _INVALID_TIMEOUT:
                await self._send_message(chat_id, "Enter a positive turn timer in seconds, or type Off/Default.", self._build_create_flow_keyboard())
                return
            flow.turn_timeout_seconds = parsed_timeout
            flow.turn_timeout_configured = True
        try:
            result = await self.backend.create_table(
                self._actor(user_id, chat_id, display_name),
                ManagedTableConfig(
                    total_seats=flow.total_seats,
                    llm_seat_count=flow.llm_seat_count,
                    small_blind=flow.small_blind,
                    big_blind=flow.big_blind,
                    ante=flow.ante,
                    starting_stack=flow.starting_stack,
                    turn_timeout_seconds=flow.turn_timeout_seconds,
                    max_hands_per_table=self.config.max_hands_per_table,
                    max_players=self.config.max_players,
                    human_transport="telegram",
                    human_seat_prefix="tg",
                ),
            )
        except BackendError as exc:
            await self._send_message(chat_id, exc.message, await self._build_lobby_keyboard(user_id=user_id, chat_id=chat_id))
            return
        finally:
            self._create_flows.pop(user_id, None)
        await self._send_message(
            chat_id,
            self._format_created_table(result["snapshot"]),
            await self._build_lobby_keyboard(user_id=user_id, chat_id=chat_id, emphasize_start=self._is_waiting_table_full(result["snapshot"])),
        )

    async def _ensure_watchers(self, snapshot: dict[str, Any], *, participants: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None = None) -> None:
        for participant in participants or snapshot.get("participants", ()):
            if participant.get("transport") != "telegram":
                continue
            metadata = participant.get("metadata", {})
            chat_id = int(metadata.get("chat_id", 0))
            if chat_id == 0:
                continue
            user_id = int(participant["external_id"])
            key = (user_id, snapshot["table_id"])
            if key in self._watchers and self._watchers[key].task is not None:
                continue
            actor = ActorRef(
                transport="telegram",
                external_id=str(user_id),
                display_name=participant["display_name"],
                metadata={"chat_id": chat_id, "user_id": user_id},
            )
            tables = await self.backend.get_actor_tables(actor)
            entry = next((item for item in tables["tables"] if item["table_id"] == snapshot["table_id"]), None)
            if entry is None:
                continue
            state = _WatcherState(
                user_id=user_id,
                chat_id=chat_id,
                table_id=snapshot["table_id"],
                viewer_token=entry["viewer_token"],
                display_name=participant["display_name"],
            )
            state.task = asyncio.create_task(self._watch_table(state))
            self._watchers[key] = state

    async def _watch_table(self, state: _WatcherState) -> None:
        try:
            initial = await self.backend.get_table_snapshot(state.table_id, state.viewer_token)
            await self._emit_watcher_messages(state, initial, new_events=[])
            version = int(initial.get("version", 0))
            while True:
                payload = await self.backend.wait_for_table_version(state.table_id, state.viewer_token, version, 15_000)
                snapshot = payload["snapshot"]
                version = int(snapshot.get("version", version))
                await self._emit_watcher_messages(state, snapshot, new_events=payload.get("new_events", []))
                if snapshot["status"] in {TelegramTableState.COMPLETED.value, TelegramTableState.CANCELLED.value}:
                    return
        except BackendError:
            return

    async def _emit_watcher_messages(self, state: _WatcherState, snapshot: dict[str, Any], *, new_events: list[dict[str, Any]]) -> None:
        player_view_payload = snapshot.get("player_view")
        public_table_payload = snapshot.get("public_table")
        if player_view_payload is not None and public_table_payload is not None:
            player_view = snapshot_player_view(player_view_payload, public_table_payload)
            status_text = render_telegram_status_panel(player_view)
            if status_text != state.last_status_text:
                state.last_status_text = status_text
                await self._send_message(state.chat_id, status_text)
        for item in snapshot.get("recent_events", []):
            event_id = item["id"]
            if event_id in state.seen_recent_event_ids:
                continue
            state.seen_recent_event_ids.add(event_id)
            if event_id.startswith("activity-"):
                await self._send_message(state.chat_id, html.unescape(str(item["text"])))
        if new_events and player_view_payload is not None and public_table_payload is not None:
            update = PlayerUpdate(
                update_type=self._infer_update_type(new_events, snapshot),
                events=tuple(game_event_from_dict(item) for item in new_events),
                public_table_view=snapshot_public_table_view(public_table_payload),
                player_view=snapshot_player_view(player_view_payload, public_table_payload),
                acting_seat_id=public_table_payload.get("acting_seat_id"),
                is_your_turn=snapshot.get("pending_decision") is not None,
            )
            for message in render_telegram_update_messages(update):
                await self._send_message(state.chat_id, message)
        pending_payload = snapshot.get("pending_decision")
        if pending_payload is None:
            state.last_prompt_signature = None
            return
        signature = json.dumps(pending_payload, sort_keys=True)
        if signature == state.last_prompt_signature:
            return
        state.last_prompt_signature = signature
        decision = snapshot_pending_decision(pending_payload, snapshot)
        await self._send_message(state.chat_id, render_telegram_turn_prompt(decision), self._build_action_keyboard(snapshot))

    async def _notify_waiting_participants(
        self,
        participants: list[dict[str, Any]] | tuple[dict[str, Any], ...],
        text: str,
        *,
        emphasize_start: bool = False,
    ) -> None:
        for participant in participants:
            if participant.get("transport") != "telegram":
                continue
            chat_id = int(participant.get("metadata", {}).get("chat_id", 0))
            if chat_id <= 0:
                continue
            await self._send_message(
                chat_id,
                text,
                await self._build_lobby_keyboard(
                    user_id=int(participant["external_id"]),
                    chat_id=chat_id,
                    emphasize_start=emphasize_start and participant.get("is_creator", False),
                ),
            )

    async def _send_message(self, chat_id: int, text: str, reply_markup: Any | None = None) -> None:
        if self._send_message_callback is not None:
            await self._send_message_callback(chat_id, text, reply_markup)
            return
        if self._bot is None:
            raise RuntimeError("TelegramApp requires either a send_message callback or an aiogram bot")
        await self._bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup, parse_mode="HTML")

    async def _actor_tables(self, *, user_id: int, chat_id: int, display_name: str) -> dict[str, Any]:
        return await self.backend.get_actor_tables(self._actor(user_id, chat_id, display_name))

    async def _primary_actor_table(self, *, user_id: int, chat_id: int, display_name: str) -> dict[str, Any] | None:
        tables = await self._actor_tables(user_id=user_id, chat_id=chat_id, display_name=display_name)
        preferred = next((item for item in tables["tables"] if item["status"] in {"waiting", "running"}), None)
        if preferred is not None:
            return preferred
        return tables["tables"][0] if tables["tables"] else None

    async def _running_actor_table(self, *, user_id: int, chat_id: int, display_name: str) -> dict[str, Any] | None:
        tables = await self._actor_tables(user_id=user_id, chat_id=chat_id, display_name=display_name)
        return next((item for item in tables["tables"] if item["status"] == "running"), None)

    def _actor(self, user_id: int, chat_id: int, display_name: str) -> ActorRef:
        return ActorRef(
            transport="telegram",
            external_id=str(user_id),
            display_name=display_name,
            metadata={"chat_id": chat_id, "user_id": user_id},
        )

    def _build_local_backend(self) -> LocalBackendClient:
        service = LocalTableBackendService(
            llm_client_factory=self._llm_client_factory,
            coach_client_factory=self._coach_client_factory,
            llm_name_allocator=self._llm_name_allocator,
            llm_recent_hand_count=self.config.llm.recent_hand_count,
            llm_thought_logging=self.config.llm.thought_logging,
            coach_enabled=self.config.coach.enabled,
            coach_recent_hand_count=self.config.coach.recent_hand_count,
        )
        return LocalBackendClient(service)

    def _sync_local_backend_settings(self) -> None:
        if not isinstance(self.backend, LocalBackendClient):
            return
        self.backend._service._llm_recent_hand_count = self.config.llm.recent_hand_count
        self.backend._service._llm_thought_logging = self.config.llm.thought_logging
        self.backend._service._coach_enabled = self.config.coach.enabled
        self.backend._service._coach_recent_hand_count = self.config.coach.recent_hand_count

    def _default_llm_client_factory(self) -> LLMGameClient:
        if self.config.llm.model is None or self.config.llm.api_key is None:
            raise RuntimeError("LLM model and API key are required to create LLM seats")
        return LLMGameClient(settings=self.config.llm)

    def _default_coach_client_factory(self) -> LLMGameClient:
        if self.config.coach.model is None or self.config.coach.api_key is None:
            raise RuntimeError("Coach model and API key are required when coach is enabled")
        return LLMGameClient(settings=self.config.coach)

    def _format_created_table(self, snapshot: dict[str, Any]) -> str:
        summary = snapshot["config_summary"]
        lines = [
            f"Created table {snapshot['table_id']}.",
            f"Total seats: {summary['total_seats']}",
            f"Telegram seats: {summary.get('telegram_seats_total', summary['human_seats'])}",
            f"LLM seats: {summary['llm_seats']}",
            f"Blinds: {summary['small_blind']}/{summary['big_blind']}",
            f"Ante: {self._format_ante(summary['ante'])}",
            f"Starting stack: {summary['starting_stack']}",
            f"Turn timer: {self._format_turn_timeout(summary['turn_timeout_seconds'])}",
        ]
        if self._has_multiple_human_players(snapshot):
            lines.append(f"Join with: /join {snapshot['table_id']}")
        if self._has_multiple_human_players(snapshot) and self.config.bot_username:
            lines.append(f"Deep link: https://t.me/{self.config.bot_username}?start=join_{snapshot['table_id']}")
        if self._is_waiting_table_full(snapshot):
            lines.append(self._format_ready_to_start_hint(snapshot))
        return "\n".join(lines)

    def _format_waiting_table_update(self, snapshot: dict[str, Any], headline: str) -> str:
        summary = snapshot["config_summary"]
        total = summary.get("telegram_seats_total", summary["human_seats"])
        claimed = summary.get("telegram_seats_claimed", summary["claimed_human_seats"])
        lines = [
            headline,
            f"Telegram seats: {claimed}/{total}.",
            f"Blinds: {summary['small_blind']}/{summary['big_blind']}.",
            f"Ante: {self._format_ante(summary['ante'])}.",
            f"Starting stack: {summary['starting_stack']}.",
            f"Turn timer: {self._format_turn_timeout(summary['turn_timeout_seconds'])}.",
        ]
        if self._is_waiting_table_full(snapshot):
            lines.append(self._format_ready_to_start_hint(snapshot))
        return "\n".join(lines)

    def _format_ready_to_start_hint(self, snapshot: dict[str, Any]) -> str:
        if self._has_multiple_human_players(snapshot):
            return f"Table {snapshot['table_id']} is ready to start. The creator can press Start Game."
        return f"Table {snapshot['table_id']} is ready to start. Press Start Game to begin."

    def _format_started_table_message(self, snapshot: dict[str, Any]) -> str:
        if self._has_multiple_human_players(snapshot):
            return f"Table {snapshot['table_id']} started with {snapshot['config_summary']['total_seats']} seats."
        return f"Table {snapshot['table_id']} started with {snapshot['config_summary']['total_seats']} seats after the creator pressed Start Game."

    def _format_status(self, snapshot: dict[str, Any]) -> str:
        summary = snapshot["config_summary"]
        joined = ", ".join(participant["external_id"] for participant in snapshot.get("participants", ()) if participant.get("transport") == "telegram") or "-"
        return "\n".join(
            [
                f"Table {snapshot['table_id']}",
                f"Status: {snapshot['status']}",
                f"Seats: {summary['total_seats']}",
                f"Blinds: {summary['small_blind']}/{summary['big_blind']}",
                f"Ante: {self._format_ante(summary['ante'])}",
                f"Starting stack: {summary['starting_stack']}",
                f"Turn timer: {self._format_turn_timeout(summary['turn_timeout_seconds'])}",
                f"Telegram seats: {summary.get('telegram_seats_claimed', summary['claimed_human_seats'])}/{summary.get('telegram_seats_total', summary['human_seats'])}",
                f"LLM seats: {summary['llm_seats']}",
                f"Joined users: {joined}",
            ]
        )

    def _build_action_keyboard(self, snapshot: dict[str, Any]) -> Any | None:
        pending = snapshot.get("pending_decision")
        if pending is None:
            return None
        labels = [[item["action_type"].replace("_", " ").title()] for item in pending.get("legal_actions", ())]
        return self._make_reply_keyboard(labels)

    @staticmethod
    def _infer_update_type(new_events: list[dict[str, Any]], snapshot: dict[str, Any]) -> PlayerUpdateType:
        if any(item["event_type"] == "table_completed" for item in new_events):
            return PlayerUpdateType.TABLE_COMPLETED
        if snapshot.get("pending_decision") is not None:
            return PlayerUpdateType.TURN_STARTED
        return PlayerUpdateType.STATE_CHANGED

    @staticmethod
    def _has_multiple_human_players(snapshot: dict[str, Any]) -> bool:
        return int(snapshot["config_summary"].get("telegram_seats_total", snapshot["config_summary"]["human_seats"])) > 1

    @staticmethod
    def _is_waiting_table_full(snapshot: dict[str, Any]) -> bool:
        summary = snapshot["config_summary"]
        return int(summary.get("telegram_seats_claimed", summary["claimed_human_seats"])) >= int(summary.get("telegram_seats_total", summary["human_seats"]))

    @staticmethod
    def _parse_int(raw: str) -> int | None:
        try:
            return int(raw.strip())
        except ValueError:
            return None

    @staticmethod
    def _parse_int_or_default(raw: str, *, default: int) -> int | None:
        if raw.strip().lower() == "default":
            return default
        return TelegramApp._parse_int(raw)

    @staticmethod
    def _parse_ante(raw: str, *, default: int) -> int | None:
        normalized = raw.strip().lower()
        if normalized in {"default", "off", "none"}:
            return default if normalized == "default" else 0
        parsed = TelegramApp._parse_int(raw)
        if parsed is None or parsed < 0:
            return None
        return parsed

    @staticmethod
    def _parse_turn_timeout(raw: str) -> int | None | object:
        normalized = raw.strip().lower()
        if normalized in {"", "off", "default", "none"}:
            return None
        parsed = TelegramApp._parse_int(raw)
        if parsed is None or parsed <= 0:
            return _INVALID_TIMEOUT
        return parsed

    @staticmethod
    def _default_small_blind(big_blind: int) -> int:
        return max(1, big_blind // 2)

    @staticmethod
    def _default_starting_stack(big_blind: int) -> int:
        return max(1, big_blind * 20)

    @staticmethod
    def _format_ante(ante: int) -> str:
        return "Off" if ante <= 0 else str(ante)

    @staticmethod
    def _format_turn_timeout(turn_timeout_seconds: int | None) -> str:
        return "Off" if turn_timeout_seconds is None else f"{turn_timeout_seconds}s"

    @staticmethod
    def _normalize_action_text(text: str) -> str | None:
        normalized = text.strip().lower()
        mapping = {
            "fold": "fold",
            "f": "fold",
            "check": "check",
            "k": "check",
            "call": "call",
            "c": "call",
            "bet": "bet",
            "b": "bet",
            "raise": "raise",
            "r": "raise",
        }
        return mapping.get(normalized)

    def _match_lobby_command(self, text: str) -> Any | None:
        normalized = text.strip().lower()
        mapping = {
            "create table": self._handle_create_from_button,
            "my table": self._handle_my_table_from_button,
            "start game": self._handle_start_game_from_button,
            "leave table": self._handle_leave_table_from_button,
            "cancel table": self._handle_cancel_table_from_button,
            "help": self._handle_help_from_button,
        }
        return mapping.get(normalized)

    async def _handle_create_from_button(self, *, user_id: int, chat_id: int, display_name: str) -> None:
        await self.handle_create_table_command(user_id=user_id, chat_id=chat_id)

    async def _handle_my_table_from_button(self, *, user_id: int, chat_id: int, display_name: str) -> None:
        await self.handle_my_table_command(user_id=user_id, chat_id=chat_id)

    async def _handle_start_game_from_button(self, *, user_id: int, chat_id: int, display_name: str) -> None:
        await self.handle_start_game_command(user_id=user_id, chat_id=chat_id)

    async def _handle_leave_table_from_button(self, *, user_id: int, chat_id: int, display_name: str) -> None:
        await self.handle_leave_table_command(user_id=user_id, chat_id=chat_id)

    async def _handle_cancel_table_from_button(self, *, user_id: int, chat_id: int, display_name: str) -> None:
        await self.handle_cancel_table_command(user_id=user_id, chat_id=chat_id)

    async def _handle_help_from_button(self, *, user_id: int, chat_id: int, display_name: str) -> None:
        del user_id, display_name
        await self.handle_help_command(chat_id=chat_id)

    async def _build_lobby_keyboard(
        self,
        user_id: int | None = None,
        chat_id: int | None = None,
        *,
        emphasize_start: bool = False,
    ) -> Any | None:
        entry = None
        if user_id is not None:
            tables = await self._actor_tables(
                user_id=user_id,
                chat_id=chat_id or 0,
                display_name=str(user_id),
            )
            entry = next((item for item in tables["tables"] if item["status"] in {"waiting", "running"}), None)
        if entry is None:
            labels = [["Create Table"], ["Help"]]
        elif entry["status"] == TelegramTableState.WAITING.value:
            if entry.get("is_creator", False):
                labels = [["My Table"], ["Start Game"], ["Cancel Table"], ["Help"]]
                if emphasize_start:
                    labels = [["Start Game"], ["My Table"], ["Cancel Table"], ["Help"]]
            else:
                labels = [["My Table"], ["Leave Table"], ["Help"]]
        elif entry["status"] == TelegramTableState.RUNNING.value:
            labels = [["My Table"], ["Leave Table"], ["Help"]]
        else:
            labels = [["Create Table"], ["Help"]]
        return self._make_reply_keyboard(labels)

    def _build_create_flow_keyboard(self) -> Any | None:
        return self._make_reply_keyboard([["Help"]])

    def _make_reply_keyboard(self, labels: list[list[str]]) -> Any | None:
        if self._bot is None:
            return labels
        try:
            from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "The aiogram package is required when TelegramApp is used with a bot instance."
            ) from exc
        return ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text=label) for label in row] for row in labels],
            resize_keyboard=True,
            one_time_keyboard=False,
        )
