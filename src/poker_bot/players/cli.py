from __future__ import annotations

from typing import Awaitable, Callable

from poker_bot.players.base import PlayerAgent
from poker_bot.players.rendering import render_decision_summary, render_player_update
from poker_bot.types import ActionType, DecisionRequest, PlayerAction, PlayerUpdate


class CLIPlayerAgent(PlayerAgent):
    def __init__(
        self,
        seat_id: str,
        input_func: Callable[[str], str] | None = None,
        output_func: Callable[[str], None] | None = None,
    ) -> None:
        self.seat_id = seat_id
        self._input = input_func or input
        self._output = output_func or print

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        self._output(render_decision_summary(decision))

        legal = {action.action_type.value: action for action in decision.legal_actions}
        while True:
            raw = self._input("Choose action: ").strip().lower()
            if raw not in legal:
                self._output("Illegal choice, please select one of the listed legal actions.")
                continue
            selected = legal[raw]
            if selected.action_type in {ActionType.BET, ActionType.RAISE}:
                amount = int(self._input("Enter total amount: ").strip())
                return PlayerAction(selected.action_type, amount=amount)
            return PlayerAction(selected.action_type)

    async def notify_update(self, update: PlayerUpdate) -> None:
        self._output(render_player_update(update))

    async def close(self) -> None:
        return None
