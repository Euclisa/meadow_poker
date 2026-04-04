from __future__ import annotations

import asyncio
import logging
from typing import Any

from poker_bot.players.base import PlayerAgent
from poker_bot.players.rendering import (
    render_telegram_status_panel,
    render_telegram_turn_prompt,
    render_telegram_update_messages,
)
from poker_bot.types import (
    ActionType,
    DecisionRequest,
    PlayerAction,
    TelegramPendingActionState,
    PlayerUpdate,
)

logger = logging.getLogger(__name__)


class TelegramPlayerAgent(PlayerAgent):
    def __init__(
        self,
        seat_id: str,
        user_id: int,
        chat_id: int,
        *,
        send_message: Any | None = None,
        bot: Any | None = None,
    ) -> None:
        self.seat_id = seat_id
        self.user_id = user_id
        self.chat_id = chat_id
        self._send_message = send_message
        self._bot = bot
        self._pending_state: TelegramPendingActionState | None = None
        self._pending_future: asyncio.Future[PlayerAction] | None = None
        self._status_message_id: int | None = None
        self._last_status_text: str | None = None

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        return await self.begin_pending_action(decision)

    async def begin_pending_action(self, decision: DecisionRequest) -> PlayerAction:
        if self._pending_future is not None:
            raise RuntimeError("A Telegram action is already pending for this seat")

        self._pending_state = TelegramPendingActionState(
            seat_id=self.seat_id,
            user_id=self.user_id,
            chat_id=self.chat_id,
            decision_request=decision,
        )
        self._pending_future = asyncio.get_running_loop().create_future()

        await self._sync_status_panel(decision.player_view)
        text = render_telegram_turn_prompt(decision)
        logger.debug("Telegram pending action created seat_id=%s user_id=%s decision=%s", self.seat_id, self.user_id, decision)
        await self._dispatch_message(text, self._build_keyboard(decision))
        try:
            return await self._pending_future
        finally:
            self._pending_future = None
            self._pending_state = None

    async def submit_text_action(self, *, user_id: int, chat_id: int, text: str) -> bool:
        if not self.matches_user(user_id=user_id, chat_id=chat_id):
            logger.debug("Telegram text action ignored seat_id=%s wrong user/chat user_id=%s chat_id=%s", self.seat_id, user_id, chat_id)
            return False
        if self._pending_state is None or self._pending_future is None or self._pending_future.done():
            logger.debug("Telegram text action ignored seat_id=%s no pending state text=%s", self.seat_id, text)
            return False
        if self._pending_state.awaiting_amount:
            return await self.submit_amount(user_id=user_id, chat_id=chat_id, amount_text=text)

        action_name = self._normalize_action_text(text)
        if action_name is None:
            logger.debug("Telegram text action ignored seat_id=%s unrecognized text=%s", self.seat_id, text)
            return False
        try:
            action_type = ActionType(action_name)
        except ValueError:
            logger.debug("Telegram text action ignored seat_id=%s invalid action_name=%s", self.seat_id, action_name)
            return False

        legal_action = self._find_legal_action(action_type)
        if legal_action is None:
            logger.debug("Telegram illegal text action seat_id=%s action_type=%s", self.seat_id, action_type)
            await self._dispatch_message("That action is not legal right now.", self._build_keyboard(self._pending_state.decision_request))
            return True

        if action_type in {ActionType.BET, ActionType.RAISE}:
            self._pending_state.selected_action_type = action_type
            self._pending_state.awaiting_amount = True
            logger.debug("Telegram awaiting amount seat_id=%s action_type=%s", self.seat_id, action_type)
            await self._dispatch_message(
                f"💬 Enter total amount for {action_type.value} "
                f"({legal_action.min_amount}-{legal_action.max_amount}).",
                None,
            )
            return True

        logger.debug("Telegram action resolved seat_id=%s action_type=%s", self.seat_id, action_type)
        self._pending_future.set_result(PlayerAction(action_type=action_type))
        return True

    async def submit_button_action(self, *, user_id: int, chat_id: int, action_name: str) -> bool:
        return await self.submit_text_action(user_id=user_id, chat_id=chat_id, text=action_name)

    async def submit_amount(self, *, user_id: int, chat_id: int, amount_text: str) -> bool:
        if not self.matches_user(user_id=user_id, chat_id=chat_id):
            logger.debug("Telegram amount ignored seat_id=%s wrong user/chat user_id=%s chat_id=%s", self.seat_id, user_id, chat_id)
            return False
        if self._pending_state is None or self._pending_future is None or self._pending_future.done():
            logger.debug("Telegram amount ignored seat_id=%s no pending amount amount_text=%s", self.seat_id, amount_text)
            return False
        if not self._pending_state.awaiting_amount or self._pending_state.selected_action_type is None:
            logger.debug("Telegram amount ignored seat_id=%s not awaiting amount", self.seat_id)
            return False

        legal_action = self._find_legal_action(self._pending_state.selected_action_type)
        if legal_action is None:
            logger.debug("Telegram amount lost legality seat_id=%s action_type=%s", self.seat_id, self._pending_state.selected_action_type)
            await self._dispatch_message("That action is no longer legal.", None)
            self._pending_state.awaiting_amount = False
            self._pending_state.selected_action_type = None
            return True

        try:
            amount = int(amount_text.strip())
        except ValueError:
            logger.debug("Telegram invalid amount text seat_id=%s amount_text=%s", self.seat_id, amount_text)
            await self._dispatch_message("🔢 Enter a numeric total amount.", None)
            return True

        if legal_action.min_amount is not None and amount < legal_action.min_amount:
            logger.debug("Telegram amount too small seat_id=%s amount=%s min=%s", self.seat_id, amount, legal_action.min_amount)
            await self._dispatch_message(f"⚠️ Amount must be at least {legal_action.min_amount}.", None)
            return True
        if legal_action.max_amount is not None and amount > legal_action.max_amount:
            logger.debug("Telegram amount too large seat_id=%s amount=%s max=%s", self.seat_id, amount, legal_action.max_amount)
            await self._dispatch_message(f"⚠️ Amount must be at most {legal_action.max_amount}.", None)
            return True

        logger.debug(
            "Telegram amount resolved seat_id=%s action_type=%s amount=%s",
            self.seat_id,
            self._pending_state.selected_action_type,
            amount,
        )
        self._pending_future.set_result(
            PlayerAction(
                action_type=self._pending_state.selected_action_type,
                amount=amount,
            )
        )
        return True

    async def cancel_pending_action(self, reason: str) -> None:
        logger.debug("Telegram pending action cancelled seat_id=%s reason=%s", self.seat_id, reason)
        if self._pending_future is not None and not self._pending_future.done():
            self._pending_future.cancel(reason)
        self._pending_future = None
        self._pending_state = None

    async def notify_update(self, update: PlayerUpdate) -> None:
        await self._sync_status_panel(update.player_view)
        for message in render_telegram_update_messages(update):
            await self._dispatch_message(message, None)

    async def close(self) -> None:
        await self.cancel_pending_action("agent_closed")

    def matches_user(self, *, user_id: int, chat_id: int) -> bool:
        return self.user_id == user_id and self.chat_id == chat_id

    def _find_legal_action(self, action_type: ActionType) -> Any | None:
        if self._pending_state is None:
            return None
        for legal_action in self._pending_state.decision_request.legal_actions:
            if legal_action.action_type == action_type:
                return legal_action
        return None

    async def _dispatch_message(self, text: str, reply_markup: Any | None) -> None:
        if self._send_message is not None:
            await self._send_message(self.chat_id, text, reply_markup)
            return
        if self._bot is None:
            raise RuntimeError("TelegramPlayerAgent requires either send_message or bot")
        await self._bot.send_message(
            chat_id=self.chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode="HTML",
        )

    async def _sync_status_panel(self, view: Any) -> None:
        text = render_telegram_status_panel(view)
        if text == self._last_status_text:
            return
        self._last_status_text = text
        if self._bot is None:
            await self._dispatch_message(text, None)
            return
        if self._status_message_id is None:
            message = await self._bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="HTML",
            )
            self._status_message_id = getattr(message, "message_id", None)
            return
        try:
            await self._bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self._status_message_id,
                text=text,
                parse_mode="HTML",
            )
        except Exception:  # pragma: no cover - Telegram fallback path
            message = await self._bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode="HTML",
            )
            self._status_message_id = getattr(message, "message_id", None)

    def _build_keyboard(self, decision: DecisionRequest) -> Any | None:
        if self._bot is None:
            return [self._button_label(item.action_type, item.min_amount, item.max_amount) for item in decision.legal_actions]
        try:
            from aiogram.types import KeyboardButton, ReplyKeyboardMarkup
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "The aiogram package is required when TelegramPlayerAgent is used with a bot instance."
            ) from exc

        keyboard = []
        for action in decision.legal_actions:
            label = self._button_label(action.action_type, action.min_amount, action.max_amount)
            keyboard.append([KeyboardButton(text=label)])
        return ReplyKeyboardMarkup(keyboard=keyboard, resize_keyboard=True, one_time_keyboard=False)

    @staticmethod
    def _button_label(action_type: ActionType, min_amount: int | None, max_amount: int | None) -> str:
        if min_amount is None:
            return action_type.value.title()
        if min_amount == max_amount:
            return f"{action_type.value.title()} {min_amount}"
        return f"{action_type.value.title()} {min_amount}-{max_amount}"

    @staticmethod
    def _normalize_action_text(text: str) -> str | None:
        normalized = text.strip().lower()
        if not normalized:
            return None
        return normalized.split()[0]
