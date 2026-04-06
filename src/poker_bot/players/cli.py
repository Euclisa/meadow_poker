from __future__ import annotations

import asyncio
import builtins
import sys
from typing import Callable

from poker_bot.players.base import PlayerAgent
from poker_bot.players.rendering import render_cli_status, render_cli_events, render_cli_standings, render_cli_turn_prompt
from poker_bot.types import ActionType, DecisionRequest, LegalAction, PlayerAction, PlayerUpdate, PlayerUpdateType

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
        self._uses_builtin_input = input_func is None or input_func is input or input_func is builtins.input

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        self._output(render_cli_status(decision.player_view))
        self._output(render_cli_turn_prompt(decision))

        legal = {action.action_type.value: action for action in decision.legal_actions}
        while True:
            raw = (await self._read_text("> ")).strip().lower()
            resolved = _ACTION_SHORTCUTS.get(raw, raw.split()[0] if raw else "")
            if resolved not in legal:
                choices = ", ".join(
                    f"[{a[0]}]{a[1:]}" for a in legal
                )
                self._output(f"  Illegal choice. Options: {choices}")
                continue
            selected = legal[resolved]
            if selected.action_type in {ActionType.BET, ActionType.RAISE}:
                amount = await self._read_amount(selected)
                if amount is None:
                    continue
                return PlayerAction(selected.action_type, amount=amount)
            return PlayerAction(selected.action_type)

    async def notify_update(self, update: PlayerUpdate) -> None:
        event_lines = render_cli_events(update)
        if event_lines:
            self._output(event_lines)
        if update.update_type == PlayerUpdateType.TABLE_COMPLETED:
            self._output(render_cli_standings(update.public_table_view))

    async def close(self) -> None:
        return None

    async def _read_amount(self, action: LegalAction) -> int | None:
        lo = action.min_amount or 0
        hi = action.max_amount or 0
        if lo == hi:
            self._output(f"  All-in: {lo}")
            return lo
        prompt = f"  Amount ({lo}-{hi}): "
        raw = (await self._read_text(prompt)).strip()
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

    async def _read_text(self, prompt: str) -> str:
        if not self._uses_builtin_input:
            return self._input(prompt)
        return await self._read_stdin_line(prompt)

    async def _read_stdin_line(self, prompt: str) -> str:
        loop = asyncio.get_running_loop()
        future: asyncio.Future[str] = loop.create_future()
        stream = sys.stdin
        fileno = stream.fileno()
        print(prompt, end="", flush=True)

        def on_readable() -> None:
            line = stream.readline()
            if not future.done():
                future.set_result(line)

        loop.add_reader(fileno, on_readable)
        try:
            return await future
        finally:
            loop.remove_reader(fileno)
