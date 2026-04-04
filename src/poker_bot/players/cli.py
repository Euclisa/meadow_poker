from __future__ import annotations

from typing import Callable

from poker_bot.players.base import PlayerAgent
from poker_bot.players.rendering import render_cli_status, render_cli_events, render_cli_turn_prompt
from poker_bot.types import ActionType, DecisionRequest, LegalAction, PlayerAction, PlayerUpdate

_ACTION_SHORTCUTS: dict[str, str] = {
    "f": "fold",
    "c": "call",
    "k": "check",
    "b": "bet",
    "r": "raise",
}


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
        self._output(render_cli_status(decision.player_view))
        self._output(render_cli_turn_prompt(decision))

        legal = {action.action_type.value: action for action in decision.legal_actions}
        while True:
            raw = self._input("> ").strip().lower()
            resolved = _ACTION_SHORTCUTS.get(raw, raw.split()[0] if raw else "")
            if resolved not in legal:
                choices = ", ".join(
                    f"[{a[0]}]{a[1:]}" for a in legal
                )
                self._output(f"  Illegal choice. Options: {choices}")
                continue
            selected = legal[resolved]
            if selected.action_type in {ActionType.BET, ActionType.RAISE}:
                amount = self._read_amount(selected)
                if amount is None:
                    continue
                return PlayerAction(selected.action_type, amount=amount)
            return PlayerAction(selected.action_type)

    async def notify_update(self, update: PlayerUpdate) -> None:
        event_lines = render_cli_events(update)
        if event_lines:
            self._output(event_lines)

    async def close(self) -> None:
        return None

    def _read_amount(self, action: LegalAction) -> int | None:
        lo = action.min_amount or 0
        hi = action.max_amount or 0
        if lo == hi:
            self._output(f"  All-in: {lo}")
            return lo
        prompt = f"  Amount ({lo}-{hi}): "
        raw = self._input(prompt).strip()
        try:
            amount = int(raw)
        except ValueError:
            self._output("  Enter a number.")
            return None
        if amount < lo:
            self._output(f"  Minimum is {lo}.")
            return None
        if amount > hi:
            self._output(f"  Maximum is {hi}.")
            return None
        return amount


