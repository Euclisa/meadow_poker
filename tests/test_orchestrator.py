from __future__ import annotations

import asyncio

from poker_bot.orchestrator import GameOrchestrator
from poker_bot.players.base import PlayerAgent
from poker_bot.poker.decks import PredefinedDeckFactory
from poker_bot.poker.engine import PokerEngine
from poker_bot.types import (
    ActionType,
    DecisionRequest,
    GameEvent,
    PlayerAction,
    PlayerView,
    PublicTableView,
    SeatConfig,
    TableConfig,
)


class ScriptedAgent(PlayerAgent):
    def __init__(self, seat_id: str, actions: list[PlayerAction]) -> None:
        self.seat_id = seat_id
        self._actions = list(actions)
        self.decisions: list[DecisionRequest] = []
        self.terminal_notifications: list[tuple[tuple[GameEvent, ...], PlayerView | PublicTableView]] = []
        self.closed = False

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        self.decisions.append(decision)
        if not self._actions:
            raise AssertionError(f"No scripted action left for {self.seat_id}")
        return self._actions.pop(0)

    async def notify_terminal(
        self,
        events: tuple[GameEvent, ...],
        view: PlayerView | PublicTableView,
    ) -> None:
        self.terminal_notifications.append((events, view))

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

    first_p1_events = tuple(event.event_type for event in agent_one.decisions[0].recent_events)
    first_p2_events = tuple(event.event_type for event in agent_two.decisions[0].recent_events)
    second_p2_events = tuple(event.event_type for event in agent_two.decisions[1].recent_events)

    assert first_p1_events == ("hand_started", "blind_posted", "blind_posted", "street_started")
    assert first_p2_events[-1] == "action_applied"
    assert "hand_started" not in second_p2_events
    assert second_p2_events[:2] == ("action_applied", "street_started")
    assert second_p2_events[-1] == "bet_updated"
    assert agent_one.terminal_notifications
    assert agent_two.terminal_notifications
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
    first_p2_events = tuple(event.event_type for event in agent_two.decisions[0].recent_events)
    assert first_p2_events.count("action_applied") == 1


def test_orchestrator_flushes_terminal_results_for_non_acting_seats() -> None:
    agent_one = ScriptedAgent("p1", actions=[PlayerAction(ActionType.FOLD)])
    agent_two = ScriptedAgent("p2", actions=[])
    orchestrator = make_heads_up_orchestrator(agent_one, agent_two)

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))

    assert len(agent_two.decisions) == 0
    assert agent_one.terminal_notifications
    assert agent_two.terminal_notifications
    notified_event_types = {
        event.event_type
        for events, _ in agent_two.terminal_notifications
        for event in events
    }
    assert "hand_awarded" in notified_event_types
    assert "hand_completed" in notified_event_types


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
    first_decision_hand_two_events = tuple(
        event.event_type for event in agent_two.decisions[0].recent_events
    )
    first_terminal_for_p2 = tuple(
        event.event_type for event in agent_two.terminal_notifications[0][0]
    )
    assert first_decision_hand_two_events == (
        "hand_started",
        "blind_posted",
        "blind_posted",
        "street_started",
    )
    assert "hand_completed" not in first_decision_hand_two_events
    assert "hand_completed" in first_terminal_for_p2
