from __future__ import annotations

from typing import Awaitable, Callable, Protocol

from meadow.types import HandRunResult


class HandRunner(Protocol):
    async def play_hand(self) -> HandRunResult:
        raise NotImplementedError

    async def complete_table(self, *, reason: str, hand_number: int) -> None:
        raise NotImplementedError

    async def close(self) -> None:
        raise NotImplementedError


async def run_table(
    runner: HandRunner,
    *,
    max_hands: int | None = None,
    close_agents: bool = True,
    after_hand: Callable[[HandRunResult], Awaitable[None]] | None = None,
) -> None:
    hands_played = 0
    hook = after_hand or _noop_after_hand
    try:
        while True:
            result = await runner.play_hand()
            if not result.started:
                break

            hands_played += 1
            await hook(result)

            if max_hands is not None and hands_played >= max_hands:
                await runner.complete_table(
                    reason="max_hands_reached",
                    hand_number=result.hand_number or hands_played,
                )
                break
    finally:
        if close_agents:
            await runner.close()


async def _noop_after_hand(result: HandRunResult) -> None:
    del result
