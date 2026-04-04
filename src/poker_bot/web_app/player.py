from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from poker_bot.players.base import PlayerAgent
from poker_bot.types import (
    ActionType,
    ActionValidationError,
    DecisionRequest,
    LegalAction,
    PlayerAction,
    PlayerUpdate,
)


@dataclass(slots=True)
class _PendingState:
    decision_request: DecisionRequest


class WebPlayerAgent(PlayerAgent):
    def __init__(
        self,
        seat_id: str,
        *,
        publish_state: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self.seat_id = seat_id
        self._publish_state = publish_state
        self._pending_state: _PendingState | None = None
        self._pending_future: asyncio.Future[PlayerAction] | None = None

    @property
    def pending_decision(self) -> DecisionRequest | None:
        if self._pending_state is None:
            return None
        return self._pending_state.decision_request

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        if self._pending_future is not None:
            raise RuntimeError("A web action is already pending for this seat")
        self._pending_state = _PendingState(decision_request=decision)
        self._pending_future = asyncio.get_running_loop().create_future()
        await self._publish()
        try:
            return await self._pending_future
        finally:
            self._pending_future = None

    async def notify_update(self, update: PlayerUpdate) -> None:
        self._pending_state = None
        await self._publish()

    async def close(self) -> None:
        await self.cancel_pending_action("agent_closed")

    async def cancel_pending_action(self, reason: str) -> None:
        if self._pending_future is not None and not self._pending_future.done():
            self._pending_future.cancel(reason)
        self._pending_future = None
        self._pending_state = None
        await self._publish()

    def submit_action(self, action: PlayerAction) -> ActionValidationError | None:
        if self._pending_state is None or self._pending_future is None or self._pending_future.done():
            return ActionValidationError("no_pending_action", "There is no pending action for this seat.")

        legal_action = self._find_legal_action(action.action_type)
        if legal_action is None:
            return ActionValidationError("illegal_action", "That action is not legal right now.")

        if action.action_type in {ActionType.BET, ActionType.RAISE}:
            if action.amount is None:
                return ActionValidationError("missing_amount", "This action requires a total amount.")
            if legal_action.min_amount is not None and action.amount < legal_action.min_amount:
                return ActionValidationError(
                    "amount_too_small",
                    f"Amount must be at least {legal_action.min_amount}.",
                )
            if legal_action.max_amount is not None and action.amount > legal_action.max_amount:
                return ActionValidationError(
                    "amount_too_large",
                    f"Amount must be at most {legal_action.max_amount}.",
                )
        self._pending_future.set_result(action)
        return None

    def _find_legal_action(self, action_type: ActionType) -> LegalAction | None:
        if self._pending_state is None:
            return None
        return next(
            (
                item
                for item in self._pending_state.decision_request.legal_actions
                if item.action_type == action_type
            ),
            None,
        )

    async def _publish(self) -> None:
        if self._publish_state is not None:
            await self._publish_state()
