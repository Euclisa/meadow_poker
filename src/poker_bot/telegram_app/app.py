from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any

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
    llm_model: str | None = None
    llm_api_key: str | None = None
    llm_base_url: str | None = None
    llm_timeout: float = 30.0
    llm_max_output_tokens: int | None = None
    max_hands_per_table: int | None = None


@dataclass(slots=True)
class _CreateTableFlowState:
    chat_id: int
    total_seats: int | None = None


class TelegramActionRouter:
    def __init__(self, registry: TelegramTableRegistry) -> None:
        self._registry = registry

    async def route_callback(self, *, user_id: int, chat_id: int, data: str) -> bool:
        logger.debug("Telegram callback received user_id=%s chat_id=%s data=%s", user_id, chat_id, data)
        session = self._registry.get_user_table(user_id)
        if session is None or session.status != TelegramTableState.RUNNING:
            return False
        parsed = self._parse_callback_data(data)
        if parsed is None:
            return False
        seat_id, action_name = parsed
        agent = session.player_agents.get(seat_id)
        if not isinstance(agent, TelegramPlayerAgent):
            return False
        return await agent.submit_button_action(
            user_id=user_id,
            chat_id=chat_id,
            action_name=action_name,
        )

    async def route_text(self, *, user_id: int, chat_id: int, text: str) -> bool:
        logger.debug("Telegram text routed user_id=%s chat_id=%s text=%s", user_id, chat_id, text)
        session = self._registry.get_user_table(user_id)
        if session is None or session.status != TelegramTableState.RUNNING:
            return False
        for agent in session.player_agents.values():
            if isinstance(agent, TelegramPlayerAgent) and agent.matches_user(user_id=user_id, chat_id=chat_id):
                return await agent.submit_amount(user_id=user_id, chat_id=chat_id, amount_text=text)
        return False

    @staticmethod
    def _parse_callback_data(data: str) -> tuple[str, str] | None:
        parts = data.split(":")
        if len(parts) != 4 or parts[:2] != ["poker", "action"]:
            return None
        return parts[2], parts[3]


class TelegramApp:
    def __init__(
        self,
        config: TelegramAppConfig,
        *,
        send_message: Any | None = None,
        llm_client_factory: Any | None = None,
        bot: Any | None = None,
        registry: TelegramTableRegistry | None = None,
    ) -> None:
        self.config = config
        self.registry = registry or TelegramTableRegistry()
        self._bot = bot
        self._send_message_callback = send_message
        self._llm_client_factory = llm_client_factory or self._default_llm_client_factory
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
        )

    async def handle_create_table_command(self, *, user_id: int, chat_id: int) -> None:
        logger.debug("Handling /create_table user_id=%s chat_id=%s", user_id, chat_id)
        if self.registry.get_user_table(user_id) is not None:
            await self._send_message(chat_id, "You are already assigned to a table.")
            return
        self._create_flows[user_id] = _CreateTableFlowState(chat_id=chat_id)
        await self._send_message(chat_id, "Enter total number of players for the table (2-6).")

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
            await self._send_message(chat_id, "Table not found.")
            return
        except ValueError as exc:
            await self._send_message(chat_id, str(exc))
            return

        await self._notify_waiting_table(
            session,
            f"{display_name} joined table {session.table_id}. "
            f"Telegram seats: {len(session.claimed_telegram_users)}/{session.telegram_seat_count}.",
        )

    async def handle_my_table_command(self, *, user_id: int, chat_id: int) -> None:
        session = self.registry.get_user_table(user_id)
        if session is None:
            await self._send_message(chat_id, "You are not assigned to any table.")
            return
        await self._send_message(chat_id, self._format_status(session))

    async def handle_start_game_command(self, *, user_id: int, chat_id: int) -> None:
        logger.debug("Handling /start_game user_id=%s chat_id=%s", user_id, chat_id)
        session = self.registry.get_user_table(user_id)
        if session is None:
            await self._send_message(chat_id, "You are not assigned to any table.")
            return
        if session.creator_user_id != user_id:
            await self._send_message(chat_id, "Only the creator can start the table.")
            return
        if session.status != TelegramTableState.WAITING:
            await self._send_message(chat_id, "Only waiting tables can be started.")
            return
        if not session.is_full():
            await self._send_message(chat_id, "All Telegram seats must be claimed before starting.")
            return

        await self._start_table(session)
        await self._notify_waiting_table(
            session,
            f"Table {session.table_id} started with {session.total_seats} seats.",
        )

    async def handle_leave_table_command(self, *, user_id: int, chat_id: int) -> None:
        logger.debug("Handling /leave_table user_id=%s chat_id=%s", user_id, chat_id)
        session = self.registry.get_user_table(user_id)
        if session is None:
            await self._send_message(chat_id, "You are not assigned to any table.")
            return
        if session.status == TelegramTableState.RUNNING:
            await self._send_message(chat_id, "Leaving a running table is not supported in v1.")
            return
        if session.creator_user_id == user_id:
            cancelled = self.registry.cancel_table(session.table_id)
            await self._notify_waiting_table(cancelled, f"Table {cancelled.table_id} was cancelled by the creator.")
            return

        self.registry.leave_waiting_table(user_id)
        await self._notify_waiting_table(
            session,
            f"User {user_id} left table {session.table_id}. "
            f"Telegram seats: {len(session.claimed_telegram_users)}/{session.telegram_seat_count}.",
        )

    async def handle_cancel_table_command(self, *, user_id: int, chat_id: int) -> None:
        logger.debug("Handling /cancel_table user_id=%s chat_id=%s", user_id, chat_id)
        session = self.registry.get_user_table(user_id)
        if session is None:
            await self._send_message(chat_id, "You are not assigned to any table.")
            return
        if session.creator_user_id != user_id:
            await self._send_message(chat_id, "Only the creator can cancel the table.")
            return
        if session.status != TelegramTableState.WAITING:
            await self._send_message(chat_id, "Only waiting tables can be cancelled.")
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

        await self._send_message(chat_id, "Unrecognized input. Use /help for commands.")

    async def run_polling(self) -> None:
        if self.config.bot_token is None:
            raise RuntimeError("bot_token is required to run Telegram polling")
        try:
            from aiogram import Bot, Dispatcher, F, Router
            from aiogram.filters import Command
            from aiogram.types import CallbackQuery, Message
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

        @router.callback_query()
        async def on_callback(callback: CallbackQuery) -> None:
            if callback.data is None:
                return
            handled = await self.handle_callback_query(
                user_id=callback.from_user.id,
                chat_id=callback.message.chat.id,
                data=callback.data,
            )
            if handled:
                await callback.answer()

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
                await self._send_message(chat_id, f"Enter a valid player count between 2 and {self.config.max_players}.")
                return
            flow.total_seats = total_seats
            await self._send_message(chat_id, f"Enter number of LLM seats (0-{total_seats - 1}).")
            return

        llm_seats = self._parse_int(text)
        assert flow.total_seats is not None
        if llm_seats is None or not 0 <= llm_seats < flow.total_seats:
            await self._send_message(chat_id, f"Enter a valid LLM seat count between 0 and {flow.total_seats - 1}.")
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
            await self._send_message(chat_id, str(exc))
            return
        finally:
            self._create_flows.pop(user_id, None)

        await self._send_message(chat_id, self._format_created_table(session))

    async def _start_table(self, session: TelegramTableSession) -> None:
        logger.debug(
            "Starting Telegram table table_id=%s total_seats=%s telegram_seats=%s llm_seats=%s users=%s",
            session.table_id,
            session.total_seats,
            session.telegram_seat_count,
            session.llm_seat_count,
            session.claimed_telegram_users,
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
                send_message=self._send_message,
                bot=self._bot,
            )

        for index in range(1, session.llm_seat_count + 1):
            seat_id = f"llm_{index}"
            seat_configs.append(SeatConfig(seat_id=seat_id, name=f"LLM {index}"))
            player_agents[seat_id] = LLMPlayerAgent(
                seat_id=seat_id,
                client=self._llm_client_factory(),
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
            logger.debug("Telegram table completed table_id=%s", session.table_id)
            self.registry.mark_completed(session)
            await self._notify_waiting_table(session, f"Table {session.table_id} has completed.")

    async def _notify_waiting_table(self, session: TelegramTableSession, text: str) -> None:
        for user in session.human_users():
            await self._send_message(user.chat_id, text)

    async def _send_message(self, chat_id: int, text: str, reply_markup: Any | None = None) -> None:
        if self._send_message_callback is not None:
            await self._send_message_callback(chat_id, text, reply_markup)
            return
        if self._bot is None:
            raise RuntimeError("TelegramApp requires either a send_message callback or an aiogram bot")
        await self._bot.send_message(chat_id=chat_id, text=text, reply_markup=reply_markup)

    def _default_llm_client_factory(self) -> LLMGameClient:
        if self.config.llm_model is None or self.config.llm_api_key is None:
            raise RuntimeError("LLM model and API key are required to create LLM seats")
        return LLMGameClient(
            model=self.config.llm_model,
            api_key=self.config.llm_api_key,
            base_url=self.config.llm_base_url,
            timeout=self.config.llm_timeout,
            max_output_tokens=self.config.llm_max_output_tokens,
        )

    def _format_created_table(self, session: TelegramTableSession) -> str:
        lines = [
            f"Created table {session.table_id}.",
            f"Total seats: {session.total_seats}",
            f"Telegram seats: {session.telegram_seat_count}",
            f"LLM seats: {session.llm_seat_count}",
            f"Join with: /join {session.table_id}",
        ]
        if self.config.bot_username:
            lines.append(f"Deep link: https://t.me/{self.config.bot_username}?start=join_{session.table_id}")
        return "\n".join(lines)

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
