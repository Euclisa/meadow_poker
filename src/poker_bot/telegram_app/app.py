from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any

from poker_bot.config import LLMSettings
from poker_bot.naming import BotNameAllocator
from poker_bot.orchestrator import GameOrchestrator
from poker_bot.players.llm import LLMGameClient, LLMPlayerAgent
from poker_bot.players.telegram import TelegramPlayerAgent
from poker_bot.poker.engine import PokerEngine
from poker_bot.telegram_app.registry import TelegramTableRegistry
from poker_bot.telegram_app.session import TelegramTableSession
from poker_bot.types import (
    SeatConfig,
    TableConfig,
    TelegramTableCreateRequest,
    TelegramTableState,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TelegramAppConfig:
    bot_token: str | None = None
    bot_username: str | None = None
    small_blind: int = 50
    big_blind: int = 100
    starting_stack: int = 2_000
    max_players: int = 6
    llm: LLMSettings = field(default_factory=LLMSettings)
    max_hands_per_table: int | None = None


@dataclass(slots=True)
class _CreateTableFlowState:
    chat_id: int
    total_seats: int | None = None


class TelegramActionRouter:
    def __init__(self, registry: TelegramTableRegistry) -> None:
        self._registry = registry

    async def route_callback(self, *, user_id: int, chat_id: int, data: str) -> bool:
        logger.debug("Telegram callback ignored after reply-keyboard migration user_id=%s chat_id=%s data=%s", user_id, chat_id, data)
        return False

    async def route_text(self, *, user_id: int, chat_id: int, text: str) -> bool:
        logger.debug("Telegram text routed user_id=%s chat_id=%s text=%s", user_id, chat_id, text)
        session = self._registry.get_user_table(user_id)
        if session is None or session.status != TelegramTableState.RUNNING:
            return False
        for agent in session.player_agents.values():
            if isinstance(agent, TelegramPlayerAgent) and agent.matches_user(user_id=user_id, chat_id=chat_id):
                return await agent.submit_text_action(user_id=user_id, chat_id=chat_id, text=text)
        return False


class TelegramApp:
    def __init__(
        self,
        config: TelegramAppConfig,
        *,
        send_message: Any | None = None,
        llm_client_factory: Any | None = None,
        llm_name_allocator: BotNameAllocator | None = None,
        bot: Any | None = None,
        registry: TelegramTableRegistry | None = None,
    ) -> None:
        self.config = config
        self.registry = registry or TelegramTableRegistry()
        self._bot = bot
        self._send_message_callback = send_message
        self._llm_client_factory = llm_client_factory or self._default_llm_client_factory
        self._llm_name_allocator = llm_name_allocator
        self._create_flows: dict[int, _CreateTableFlowState] = {}
        self.action_router = TelegramActionRouter(self.registry)

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
            "Welcome to Poker Bot.\nUse /create_table to start a table or /join <table_id> to join one.",
            self._build_lobby_keyboard(user_id),
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
                    "/help - show this help",
                ]
            ),
            self._build_lobby_keyboard(),
        )

    async def handle_create_table_command(self, *, user_id: int, chat_id: int) -> None:
        logger.debug("Handling /create_table user_id=%s chat_id=%s", user_id, chat_id)
        if self.registry.get_user_table(user_id) is not None:
            await self._send_message(chat_id, "You are already assigned to a table.")
            return
        self._create_flows[user_id] = _CreateTableFlowState(chat_id=chat_id)
        await self._send_message(chat_id, "Enter total number of players for the table (2-6).", self._build_create_flow_keyboard())

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
            session = self.registry.join_table(
                table_id=table_id,
                user_id=user_id,
                chat_id=chat_id,
                display_name=display_name,
            )
        except KeyError:
            await self._send_message(chat_id, "Table not found.", self._build_lobby_keyboard(user_id))
            return
        except ValueError as exc:
            await self._send_message(chat_id, str(exc), self._build_lobby_keyboard(user_id))
            return

        await self._notify_waiting_table(
            session,
            self._format_waiting_table_update(
                session,
                f"{display_name} joined table {session.table_id}.",
            ),
            emphasize_start=session.is_full(),
        )

    async def handle_my_table_command(self, *, user_id: int, chat_id: int) -> None:
        session = self.registry.get_user_table(user_id)
        if session is None:
            await self._send_message(chat_id, "You are not assigned to any table.")
            return
        await self._send_message(chat_id, self._format_status(session), self._build_lobby_keyboard(user_id))

    async def handle_start_game_command(self, *, user_id: int, chat_id: int) -> None:
        logger.debug("Handling /start_game user_id=%s chat_id=%s", user_id, chat_id)
        session = self.registry.get_user_table(user_id)
        if session is None:
            await self._send_message(chat_id, "You are not assigned to any table.", self._build_lobby_keyboard(user_id))
            return
        if session.creator_user_id != user_id:
            await self._send_message(chat_id, "Only the creator can start the table.", self._build_lobby_keyboard(user_id))
            return
        if session.status != TelegramTableState.WAITING:
            await self._send_message(chat_id, "Only waiting tables can be started.", self._build_lobby_keyboard(user_id))
            return
        if not session.is_full():
            await self._send_message(chat_id, "All Telegram seats must be claimed before starting.", self._build_lobby_keyboard(user_id))
            return

        await self._start_table(session)
        await self._notify_waiting_table(
            session,
            self._format_started_table_message(session),
        )

    async def handle_leave_table_command(self, *, user_id: int, chat_id: int) -> None:
        logger.debug("Handling /leave_table user_id=%s chat_id=%s", user_id, chat_id)
        session = self.registry.get_user_table(user_id)
        if session is None:
            await self._send_message(chat_id, "You are not assigned to any table.", self._build_lobby_keyboard(user_id))
            return
        if session.status == TelegramTableState.RUNNING:
            await self._send_message(chat_id, "Leaving a running table is not supported in v1.", self._build_lobby_keyboard(user_id))
            return
        if session.creator_user_id == user_id:
            cancelled = self.registry.cancel_table(session.table_id)
            await self._notify_waiting_table(cancelled, f"Table {cancelled.table_id} was cancelled by the creator.")
            return

        reservation = next(
            (u for u in session.claimed_telegram_users if u.user_id == user_id), None
        )
        display_name = reservation.display_name if reservation else str(user_id)
        self.registry.leave_waiting_table(user_id)
        await self._notify_waiting_table(
            session,
            self._format_waiting_table_update(
                session,
                f"{display_name} left table {session.table_id}.",
            ),
        )

    async def handle_cancel_table_command(self, *, user_id: int, chat_id: int) -> None:
        logger.debug("Handling /cancel_table user_id=%s chat_id=%s", user_id, chat_id)
        session = self.registry.get_user_table(user_id)
        if session is None:
            await self._send_message(chat_id, "You are not assigned to any table.", self._build_lobby_keyboard(user_id))
            return
        if session.creator_user_id != user_id:
            await self._send_message(chat_id, "Only the creator can cancel the table.", self._build_lobby_keyboard(user_id))
            return
        if session.status != TelegramTableState.WAITING:
            await self._send_message(chat_id, "Only waiting tables can be cancelled.", self._build_lobby_keyboard(user_id))
            return
        cancelled = self.registry.cancel_table(session.table_id)
        await self._notify_waiting_table(cancelled, f"Table {cancelled.table_id} was cancelled.")

    async def handle_callback_query(self, *, user_id: int, chat_id: int, data: str) -> bool:
        return await self.action_router.route_callback(user_id=user_id, chat_id=chat_id, data=data)

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
            await self._send_message(chat_id, "Table creation cancelled.", self._build_lobby_keyboard(user_id))
            return
        if user_id in self._create_flows:
            await self._handle_create_flow_step(
                user_id=user_id,
                chat_id=chat_id,
                display_name=display_name,
                text=text,
            )
            return

        consumed = await self.action_router.route_text(user_id=user_id, chat_id=chat_id, text=text)
        if consumed:
            return

        await self._send_message(chat_id, "Unrecognized input. Use /help for commands.", self._build_lobby_keyboard(user_id))

    async def run_polling(self) -> None:
        if self.config.bot_token is None:
            raise RuntimeError("bot_token is required to run Telegram polling")
        try:
            from aiogram import Bot, Dispatcher, F, Router
            from aiogram.filters import Command
            from aiogram.types import Message
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "The aiogram package is required for Telegram mode. Install poker-bot[telegram]."
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

    async def _handle_create_flow_step(
        self,
        *,
        user_id: int,
        chat_id: int,
        display_name: str,
        text: str,
    ) -> None:
        logger.debug("Create flow step user_id=%s chat_id=%s text=%s", user_id, chat_id, text)
        flow = self._create_flows[user_id]
        if flow.total_seats is None:
            total_seats = self._parse_int(text)
            if total_seats is None or not 2 <= total_seats <= self.config.max_players:
                await self._send_message(chat_id, f"Enter a valid player count between 2 and {self.config.max_players}.", self._build_create_flow_keyboard())
                return
            flow.total_seats = total_seats
            await self._send_message(chat_id, f"Enter number of LLM seats (0-{total_seats - 1}).", self._build_create_flow_keyboard())
            return

        llm_seats = self._parse_int(text)
        assert flow.total_seats is not None
        if llm_seats is None or not 0 <= llm_seats < flow.total_seats:
            await self._send_message(chat_id, f"Enter a valid LLM seat count between 0 and {flow.total_seats - 1}.", self._build_create_flow_keyboard())
            return

        request = TelegramTableCreateRequest(total_seats=flow.total_seats, llm_seat_count=llm_seats)
        try:
            session = self.registry.create_waiting_table(
                creator_user_id=user_id,
                creator_chat_id=chat_id,
                creator_name=display_name,
                request=request,
            )
        except ValueError as exc:
            await self._send_message(chat_id, str(exc), self._build_lobby_keyboard(user_id))
            return
        finally:
            self._create_flows.pop(user_id, None)

        await self._send_message(
            chat_id,
            self._format_created_table(session),
            self._build_lobby_keyboard(user_id, emphasize_start=session.is_full()),
        )

    async def _start_table(self, session: TelegramTableSession) -> None:
        logger.info(
            "Starting table %s seats=%s telegram=%s llm=%s",
            session.table_id,
            session.total_seats,
            session.telegram_seat_count,
            session.llm_seat_count,
        )
        seat_configs: list[SeatConfig] = []
        player_agents: dict[str, Any] = {}

        for index, user in enumerate(session.claimed_telegram_users, start=1):
            seat_id = f"tg_{index}"
            seat_configs.append(SeatConfig(seat_id=seat_id, name=user.display_name))
            player_agents[seat_id] = TelegramPlayerAgent(
                seat_id=seat_id,
                user_id=user.user_id,
                chat_id=user.chat_id,
                send_message=None if self._bot is not None else self._send_message,
                bot=self._bot,
            )

        for index in range(1, session.llm_seat_count + 1):
            seat_id = f"llm_{index}"
            seat_configs.append(SeatConfig(seat_id=seat_id, name=self._allocate_llm_name()))
            player_agents[seat_id] = LLMPlayerAgent(
                seat_id=seat_id,
                client=self._llm_client_factory(),
                recent_hand_count=self.config.llm.recent_hand_count,
                log_thoughts=self.config.llm.log_thoughts,
            )

        engine = PokerEngine.create_table(
            TableConfig(
                small_blind=self.config.small_blind,
                big_blind=self.config.big_blind,
                starting_stack=self.config.starting_stack,
            ),
            seat_configs,
        )
        orchestrator = GameOrchestrator(engine, player_agents)
        session.engine = engine
        session.player_agents = player_agents
        session.orchestrator = orchestrator
        self.registry.mark_running(session)
        session.orchestrator_task = asyncio.create_task(self._run_session(session))

    async def _run_session(self, session: TelegramTableSession) -> None:
        assert session.orchestrator is not None
        try:
            await session.orchestrator.run(max_hands=self.config.max_hands_per_table, close_agents=True)
        finally:
            logger.info("Table %s completed", session.table_id)
            self.registry.mark_completed(session)
            await self._notify_waiting_table(session, f"Table {session.table_id} has completed.")

    async def _notify_waiting_table(
        self,
        session: TelegramTableSession,
        text: str,
        *,
        emphasize_start: bool = False,
    ) -> None:
        for user in session.human_users():
            await self._send_message(
                user.chat_id,
                text,
                self._build_lobby_keyboard(user.user_id, emphasize_start=emphasize_start),
            )

    async def _send_message(self, chat_id: int, text: str, reply_markup: Any | None = None) -> None:
        if self._send_message_callback is not None:
            await self._send_message_callback(chat_id, text, reply_markup)
            return
        if self._bot is None:
            raise RuntimeError("TelegramApp requires either a send_message callback or an aiogram bot")
        await self._bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    def _default_llm_client_factory(self) -> LLMGameClient:
        if self.config.llm.model is None or self.config.llm.api_key is None:
            raise RuntimeError("LLM model and API key are required to create LLM seats")
        return LLMGameClient(
            settings=self.config.llm,
        )

    def _allocate_llm_name(self) -> str:
        if self._llm_name_allocator is None:
            self._llm_name_allocator = BotNameAllocator()
        return self._llm_name_allocator.allocate()

    def _format_created_table(self, session: TelegramTableSession) -> str:
        lines = [
            f"Created table {session.table_id}.",
            f"Total seats: {session.total_seats}",
            f"Telegram seats: {session.telegram_seat_count}",
            f"LLM seats: {session.llm_seat_count}",
        ]
        if session.has_multiple_human_players:
            lines.append(f"Join with: /join {session.table_id}")
        if session.has_multiple_human_players and self.config.bot_username:
            lines.append(f"Deep link: https://t.me/{self.config.bot_username}?start=join_{session.table_id}")
        if session.is_full():
            lines.append(self._format_ready_to_start_hint(session))
        return "\n".join(lines)

    def _format_waiting_table_update(self, session: TelegramTableSession, headline: str) -> str:
        lines = [
            headline,
            f"Telegram seats: {session.human_player_count}/{session.telegram_seat_count}.",
        ]
        if session.is_full():
            lines.append(self._format_ready_to_start_hint(session))
        return "\n".join(lines)

    def _format_ready_to_start_hint(self, session: TelegramTableSession) -> str:
        if session.has_multiple_human_players:
            return f"Table {session.table_id} is ready to start. The creator can press Start Game."
        return f"Table {session.table_id} is ready to start. Press Start Game to begin."

    def _format_started_table_message(self, session: TelegramTableSession) -> str:
        if session.has_multiple_human_players:
            return f"Table {session.table_id} started with {session.total_seats} seats."
        return f"Table {session.table_id} started with {session.total_seats} seats after the creator pressed Start Game."

    def _format_status(self, session: TelegramTableSession) -> str:
        status = session.status_view()
        return "\n".join(
            [
                f"Table {status.table_id}",
                f"Status: {status.status.value}",
                f"Creator: {status.creator_user_id}",
                f"Seats: {status.total_seats}",
                f"Telegram seats: {status.telegram_seats_claimed}/{status.telegram_seats_total}",
                f"LLM seats: {status.llm_seat_count}",
                f"Joined users: {', '.join(str(user_id) for user_id in status.joined_user_ids) or '-'}",
            ]
        )

    @staticmethod
    def _parse_int(raw: str) -> int | None:
        try:
            return int(raw.strip())
        except ValueError:
            return None

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
        await self.handle_help_command(chat_id=chat_id)

    def _build_lobby_keyboard(self, user_id: int | None = None, *, emphasize_start: bool = False) -> Any | None:
        session = self.registry.get_user_table(user_id) if user_id is not None else None
        if session is None:
            labels = [["Create Table"], ["Help"]]
        elif session.status == TelegramTableState.WAITING:
            if session.creator_user_id == user_id:
                if emphasize_start and session.is_full():
                    labels = [["Start Game"], ["My Table"], ["Cancel Table"], ["Help"]]
                else:
                    labels = [["My Table"], ["Start Game"], ["Cancel Table"], ["Help"]]
            else:
                labels = [["My Table"], ["Leave Table"], ["Help"]]
        elif session.status == TelegramTableState.RUNNING:
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
