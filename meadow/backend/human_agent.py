from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable

from meadow.player_agent import PlayerAgent
from meadow.types import ActionType, ActionValidationError, DecisionRequest, PlayerAction, PlayerUpdate


@dataclass(slots=True)
class _PendingState:
    decision_request: DecisionRequest


class BackendHumanAgent(PlayerAgent):
    def __init__(
        self,
        seat_id: str,
        *,
        on_state_changed: Callable[[], Awaitable[None]],
    ) -> None:
        self.seat_id = seat_id
        self._on_state_changed = on_state_changed
        self._pending_state: _PendingState | None = None
        self._pending_future: asyncio.Future[PlayerAction] | None = None

    @property
    def keeps_table_alive(self) -> bool:
        return True

    @property
    def auto_sit_out_on_timeout(self) -> bool:
        return True

    @property
    def pending_decision(self) -> DecisionRequest | None:
        if self._pending_state is None:
            return None
        return self._pending_state.decision_request

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        if self._pending_future is not None:
            raise RuntimeError("A human action is already pending for this seat")
        self._pending_state = _PendingState(decision_request=decision)
        self._pending_future = asyncio.get_running_loop().create_future()
        await self._on_state_changed()
        try:
            return await self._pending_future
        finally:
            self._pending_future = None
            self._pending_state = None

    async def notify_update(self, update: PlayerUpdate) -> None:
        del update
        self._pending_state = None
        await self._on_state_changed()

    async def close(self) -> None:
        if self._pending_future is not None and not self._pending_future.done():
            self._pending_future.cancel("agent_closed")
        self._pending_future = None
        self._pending_state = None
        await self._on_state_changed()

    def cancel_pending(self, *, reason: str) -> bool:
        if self._pending_future is None or self._pending_future.done():
            return False
        self._pending_future.cancel(reason)
        return True

    def submit_action(self, action: PlayerAction) -> ActionValidationError | None:
        if self._pending_state is None or self._pending_future is None or self._pending_future.done():
            return ActionValidationError("no_pending_action", "There is no pending action for this seat.")
        legal_action = next(
            (
                item
                for item in self._pending_state.decision_request.legal_actions
                if item.action_type == action.action_type
            ),
            None,
        )
        if legal_action is None:
            return ActionValidationError("illegal_action", "That action is not legal right now.")
        if action.action_type in {ActionType.BET, ActionType.RAISE}:
            if action.amount is None:
                return ActionValidationError("missing_amount", "This action requires a total amount.")
            if legal_action.min_amount is not None and action.amount < legal_action.min_amount:
                return ActionValidationError("amount_too_small", f"Amount must be at least {legal_action.min_amount}.")
            if legal_action.max_amount is not None and action.amount > legal_action.max_amount:
                return ActionValidationError("amount_too_large", f"Amount must be at most {legal_action.max_amount}.")
        self._pending_future.set_result(action)
        return None
