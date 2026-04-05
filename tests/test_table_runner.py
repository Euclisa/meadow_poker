from __future__ import annotations

import asyncio

from poker_bot.table_runner import run_table
from poker_bot.types import HandRunResult


class FakeHandRunner:
    def __init__(self, results: list[HandRunResult]) -> None:
        self._results = list(results)
        self.play_calls = 0
        self.completed: list[tuple[str, int]] = []
        self.closed = False

    async def play_hand(self) -> HandRunResult:
        self.play_calls += 1
        return self._results.pop(0)

    async def complete_table(self, *, reason: str, hand_number: int) -> None:
        self.completed.append((reason, hand_number))

    async def close(self) -> None:
        self.closed = True


def test_run_table_calls_after_hand_once_per_completed_hand() -> None:
    runner = FakeHandRunner(
        [
            HandRunResult(started=True, hand_number=1, ended_in_showdown=False, table_complete=False),
            HandRunResult(started=True, hand_number=2, ended_in_showdown=True, table_complete=False),
            HandRunResult(started=False, hand_number=None, ended_in_showdown=False, table_complete=True),
        ]
    )
    seen: list[int | None] = []

    async def exercise() -> None:
        await run_table(
            runner,
            close_agents=False,
            after_hand=lambda result: _record_hand(result, seen),
        )

    asyncio.run(exercise())

    assert seen == [1, 2]
    assert runner.play_calls == 3
    assert runner.closed is False


def test_run_table_stops_on_max_hands_and_marks_completion() -> None:
    runner = FakeHandRunner(
        [
            HandRunResult(started=True, hand_number=4, ended_in_showdown=False, table_complete=False),
            HandRunResult(started=True, hand_number=5, ended_in_showdown=False, table_complete=False),
        ]
    )

    asyncio.run(run_table(runner, max_hands=1, close_agents=False))

    assert runner.play_calls == 1
    assert runner.completed == [("max_hands_reached", 4)]


def test_run_table_stops_cleanly_when_no_next_hand_can_start() -> None:
    runner = FakeHandRunner(
        [
            HandRunResult(started=False, hand_number=None, ended_in_showdown=False, table_complete=True),
        ]
    )

    asyncio.run(run_table(runner))

    assert runner.play_calls == 1
    assert runner.completed == []
    assert runner.closed is True


async def _record_hand(result: HandRunResult, seen: list[int | None]) -> None:
    seen.append(result.hand_number)
