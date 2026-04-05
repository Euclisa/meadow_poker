from __future__ import annotations

import asyncio

from poker_bot.orchestrator import GameOrchestrator
from poker_bot.players.base import PlayerAgent
from poker_bot.poker.decks import PredefinedDeckFactory
from poker_bot.poker.engine import PokerEngine
from poker_bot.types import (
    ActionType,
    DecisionRequest,
    PlayerAction,
    PlayerUpdate,
    PlayerUpdateType,
    SeatConfig,
    TableConfig,
)


class ScriptedAgent(PlayerAgent):
    def __init__(self, seat_id: str, actions: list[PlayerAction]) -> None:
        self.seat_id = seat_id
        self._actions = list(actions)
        self.decisions: list[DecisionRequest] = []
        self.update_counts_at_decision: list[int] = []
        self.updates: list[PlayerUpdate] = []
        self.closed = False

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        self.decisions.append(decision)
        self.update_counts_at_decision.append(len(self.updates))
        if not self._actions:
            raise AssertionError(f"No scripted action left for {self.seat_id}")
        return self._actions.pop(0)

    async def notify_update(self, update: PlayerUpdate) -> None:
        self.updates.append(update)

    async def close(self) -> None:
        self.closed = True


class AlternateScriptedAgent(ScriptedAgent):
    pass


def make_heads_up_orchestrator(agent_one: ScriptedAgent, agent_two: ScriptedAgent) -> GameOrchestrator:
    deck = (
        "As",
        "Kh",
        "Ad",
        "Kd",
        "2c",
        "7d",
        "8h",
        "9s",
        "Tc",
    )
    engine = PokerEngine.create_table(
        TableConfig(deck_factory=PredefinedDeckFactory([deck])),
        [SeatConfig("p1", "P1"), SeatConfig("p2", "P2")],
    )
    return GameOrchestrator(engine, {"p1": agent_one, "p2": agent_two})


def test_orchestrator_only_prompts_acting_seat_and_delivers_event_deltas() -> None:
    agent_one = ScriptedAgent(
        "p1",
        actions=[
            PlayerAction(ActionType.CALL),
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
        ],
    )
    agent_two = ScriptedAgent(
        "p2",
        actions=[
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
        ],
    )
    orchestrator = make_heads_up_orchestrator(agent_one, agent_two)

    asyncio.run(orchestrator.run(max_hands=1))

    assert len(agent_one.decisions) == 4
    assert len(agent_two.decisions) == 4
    assert agent_one.updates
    assert agent_two.updates
    first_p1_events = tuple(event.event_type for event in agent_one.updates[0].events)
    first_p2_events = tuple(event.event_type for event in agent_two.updates[1].events)
    second_p2_events = tuple(event.event_type for event in agent_two.updates[2].events)

    assert first_p1_events == ("hand_started", "blind_posted", "blind_posted", "street_started")
    assert agent_one.updates[0].update_type == PlayerUpdateType.TURN_STARTED
    assert agent_two.updates[0].update_type == PlayerUpdateType.STATE_CHANGED
    assert first_p2_events == ("action_applied",)
    assert second_p2_events[0] == "action_applied"
    assert "street_started" in second_p2_events
    assert agent_one.closed is True
    assert agent_two.closed is True


def test_orchestrator_retries_same_seat_after_invalid_action() -> None:
    agent_one = ScriptedAgent(
        "p1",
        actions=[
            PlayerAction(ActionType.BET, amount=300),
            PlayerAction(ActionType.CALL),
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
        ],
    )
    agent_two = ScriptedAgent(
        "p2",
        actions=[
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
        ],
    )
    orchestrator = make_heads_up_orchestrator(agent_one, agent_two)

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))

    assert len(agent_one.decisions) == 5
    assert agent_one.decisions[1].validation_error is not None
    assert agent_one.decisions[1].validation_error.code == "illegal_action"
    assert len(agent_two.decisions) == 4
    assert agent_one.update_counts_at_decision[1] == agent_one.update_counts_at_decision[0]


def test_orchestrator_play_hand_returns_started_false_when_no_hand_can_begin() -> None:
    engine = PokerEngine.create_table(
        TableConfig(deck_factory=PredefinedDeckFactory([])),
        [SeatConfig("p1", "P1"), SeatConfig("p2", "P2")],
    )
    agent_one = ScriptedAgent("p1", actions=[])
    agent_two = ScriptedAgent("p2", actions=[])
    orchestrator = GameOrchestrator(engine, {"p1": agent_one, "p2": agent_two})

    result = asyncio.run(orchestrator.play_hand())

    assert result.started is False
    assert result.hand_number is None
    assert result.table_complete is True
    assert any(event.event_type == "table_completed" for event in result.events)


def test_orchestrator_play_hand_marks_showdown_only_for_real_showdown() -> None:
    showdown_orchestrator = make_heads_up_orchestrator(
        ScriptedAgent(
            "p1",
            actions=[
                PlayerAction(ActionType.CALL),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
            ],
        ),
        ScriptedAgent(
            "p2",
            actions=[
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
            ],
        ),
    )
    fold_orchestrator = make_heads_up_orchestrator(
        ScriptedAgent("p1", actions=[PlayerAction(ActionType.FOLD)]),
        ScriptedAgent("p2", actions=[]),
    )

    showdown_result = asyncio.run(showdown_orchestrator.play_hand())
    fold_result = asyncio.run(fold_orchestrator.play_hand())

    assert showdown_result.started is True
    assert showdown_result.ended_in_showdown is True
    assert fold_result.started is True
    assert fold_result.ended_in_showdown is False


def test_orchestrator_flushes_terminal_results_for_non_acting_seats() -> None:
    agent_one = ScriptedAgent("p1", actions=[PlayerAction(ActionType.FOLD)])
    agent_two = ScriptedAgent("p2", actions=[])
    orchestrator = make_heads_up_orchestrator(agent_one, agent_two)

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))

    assert len(agent_two.decisions) == 0
    assert agent_one.updates
    assert agent_two.updates
    notified_event_types = {
        event.event_type
        for update in agent_two.updates
        for event in update.events
    }
    assert "hand_awarded" in notified_event_types
    assert "hand_completed" in notified_event_types
    assert any(update.update_type == PlayerUpdateType.HAND_COMPLETED for update in agent_two.updates)


def test_orchestrator_flushes_hand_end_events_before_next_hand_prompt() -> None:
    engine = PokerEngine.create_table(
        TableConfig(
            deck_factory=PredefinedDeckFactory(
                [
                    ("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),
                    ("Qs", "Jh", "Qd", "Jd", "2h", "3c", "4d", "5s", "6c"),
                ]
            )
        ),
        [SeatConfig("p1", "P1"), SeatConfig("p2", "P2")],
    )
    agent_one = ScriptedAgent("p1", actions=[PlayerAction(ActionType.FOLD)])
    agent_two = AlternateScriptedAgent("p2", actions=[PlayerAction(ActionType.FOLD)])
    orchestrator = GameOrchestrator(engine, {"p1": agent_one, "p2": agent_two})

    asyncio.run(orchestrator.run(max_hands=2, close_agents=False))

    assert len(agent_one.decisions) == 1
    assert len(agent_two.decisions) == 1
    hand_completed_updates = [update for update in agent_two.updates if update.update_type == PlayerUpdateType.HAND_COMPLETED]
    next_hand_updates = [
        update for update in agent_two.updates if any(event.event_type == "hand_started" and event.payload.get("hand_number") == 2 for event in update.events)
    ]
    assert hand_completed_updates
    assert next_hand_updates
    assert tuple(event.event_type for event in next_hand_updates[0].events) == (
        "hand_started",
        "blind_posted",
        "blind_posted",
        "street_started",
    )


def test_orchestrator_marks_table_completed_updates() -> None:
    agent_one = ScriptedAgent("p1", actions=[PlayerAction(ActionType.FOLD)])
    agent_two = ScriptedAgent("p2", actions=[])
    deck = ("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc")
    engine = PokerEngine.create_table(
        TableConfig(deck_factory=PredefinedDeckFactory([deck])),
        [SeatConfig("p1", "P1"), SeatConfig("p2", "P2")],
    )
    orchestrator = GameOrchestrator(engine, {"p1": agent_one, "p2": agent_two})

    asyncio.run(orchestrator.run(max_hands=2, close_agents=False))

    assert any(update.update_type == PlayerUpdateType.TABLE_COMPLETED for update in agent_one.updates)
    assert any(update.update_type == PlayerUpdateType.TABLE_COMPLETED for update in agent_two.updates)
