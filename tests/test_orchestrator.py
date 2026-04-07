from __future__ import annotations

import asyncio
from typing import Any

from meadow.orchestrator import GameOrchestrator, resolve_fallback_action
from meadow.player_agent import PlayerAgent
from meadow.poker.decks import DeckSequenceFactory
from meadow.poker.engine import PokerEngine
from meadow.types import (
    ActionType,
    DecisionRequest,
    GamePhase,
    HandRecordStatus,
    LegalAction,
    PlayerAction,
    PlayerUpdate,
    PlayerUpdateType,
    SeatConfig,
    TableConfig,
)


class ScriptedAgent(PlayerAgent):
    def __init__(self, seat_id: str, actions: list[PlayerAction], *, keeps_table_alive: bool = True) -> None:
        self.seat_id = seat_id
        self._actions = list(actions)
        self._keeps_table_alive = keeps_table_alive
        self.decisions: list[DecisionRequest] = []
        self.update_counts_at_decision: list[int] = []
        self.updates: list[PlayerUpdate] = []
        self.completed_hand_records: list[tuple] = []
        self.closed = False

    @property
    def keeps_table_alive(self) -> bool:
        return self._keeps_table_alive

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        self.decisions.append(decision)
        self.update_counts_at_decision.append(len(self.updates))
        if not self._actions:
            raise AssertionError(f"No scripted action left for {self.seat_id}")
        return self._actions.pop(0)

    async def notify_update(self, update: PlayerUpdate) -> None:
        self.updates.append(update)

    async def on_hand_completed(self, record: Any, player_view: Any) -> None:
        self.completed_hand_records.append((record, player_view))

    async def close(self) -> None:
        self.closed = True


class AlternateScriptedAgent(ScriptedAgent):
    pass


class InspectingScriptedAgent(ScriptedAgent):
    def __init__(self, seat_id: str, actions: list[PlayerAction]) -> None:
        super().__init__(seat_id, actions)
        self.orchestrator: GameOrchestrator | None = None
        self.hand_records_at_decision = []

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        self.hand_records_at_decision.append(self.orchestrator.current_hand_record if self.orchestrator is not None else None)
        return await super().request_action(decision)


class SlowScriptedAgent(ScriptedAgent):
    def __init__(
        self,
        seat_id: str,
        actions: list[PlayerAction],
        *,
        slow_indices: set[int],
        delay_seconds: float,
        keeps_table_alive: bool = True,
    ) -> None:
        super().__init__(seat_id, actions, keeps_table_alive=keeps_table_alive)
        self._slow_indices = slow_indices
        self._delay_seconds = delay_seconds

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        decision_index = len(self.decisions)
        if decision_index in self._slow_indices:
            self.decisions.append(decision)
            self.update_counts_at_decision.append(len(self.updates))
            await asyncio.sleep(self._delay_seconds)
            if not self._actions:
                raise AssertionError(f"No scripted action left for {self.seat_id}")
            return self._actions.pop(0)
        return await super().request_action(decision)


class SlowHumanScriptedAgent(SlowScriptedAgent):
    @property
    def auto_sit_out_on_timeout(self) -> bool:
        return True


class TimerInspectingAgent(ScriptedAgent):
    def __init__(self, seat_id: str, actions: list[PlayerAction]) -> None:
        super().__init__(seat_id, actions)
        self.orchestrator: GameOrchestrator | None = None
        self.timer_starts: list[float | None] = []

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        timer = self.orchestrator.current_turn_timer if self.orchestrator is not None else None
        self.timer_starts.append(None if timer is None else timer.started_monotonic)
        return await super().request_action(decision)


class RaisingAgent(PlayerAgent):
    def __init__(self, seat_id: str, *, exc: Exception, keeps_table_alive: bool = True) -> None:
        self.seat_id = seat_id
        self.exc = exc
        self._keeps_table_alive = keeps_table_alive
        self.updates: list[PlayerUpdate] = []

    @property
    def keeps_table_alive(self) -> bool:
        return self._keeps_table_alive

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        raise self.exc

    async def notify_update(self, update: PlayerUpdate) -> None:
        self.updates.append(update)

    async def close(self) -> None:
        return None


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
        TableConfig(deck_factory=DeckSequenceFactory([deck])),
        [SeatConfig("p1", "P1"), SeatConfig("p2", "P2")],
    )
    orchestrator = GameOrchestrator(engine, {"p1": agent_one, "p2": agent_two})
    for agent in (agent_one, agent_two):
        if isinstance(agent, InspectingScriptedAgent):
            agent.orchestrator = orchestrator
        if isinstance(agent, TimerInspectingAgent):
            agent.orchestrator = orchestrator
    return orchestrator


def test_resolve_fallback_action_prefers_check_then_fold() -> None:
    assert resolve_fallback_action((LegalAction(ActionType.CHECK),)).action_type is ActionType.CHECK
    assert resolve_fallback_action((LegalAction(ActionType.FOLD), LegalAction(ActionType.CALL))).action_type is ActionType.FOLD


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
        TableConfig(deck_factory=DeckSequenceFactory([])),
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
            deck_factory=DeckSequenceFactory(
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
        TableConfig(deck_factory=DeckSequenceFactory([deck])),
        [SeatConfig("p1", "P1"), SeatConfig("p2", "P2")],
    )
    orchestrator = GameOrchestrator(engine, {"p1": agent_one, "p2": agent_two})

    asyncio.run(orchestrator.run(max_hands=2, close_agents=False))

    assert any(update.update_type == PlayerUpdateType.TABLE_COMPLETED for update in agent_one.updates)
    assert any(update.update_type == PlayerUpdateType.TABLE_COMPLETED for update in agent_two.updates)


def test_orchestrator_exposes_live_and_completed_hand_records() -> None:
    agent_one = InspectingScriptedAgent(
        "p1",
        actions=[
            PlayerAction(ActionType.CALL),
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
        ],
    )
    agent_two = InspectingScriptedAgent(
        "p2",
        actions=[
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
        ],
    )
    orchestrator = make_heads_up_orchestrator(agent_one, agent_two)

    result = asyncio.run(orchestrator.play_hand())

    assert agent_one.hand_records_at_decision
    live_record = agent_one.hand_records_at_decision[0]
    assert live_record is not None
    assert live_record.status is HandRecordStatus.IN_PROGRESS
    assert live_record.hand_number == 1
    assert live_record.start_public_view.hand_number == 1
    assert any(event.event_type == "hand_started" for event in live_record.events)

    assert result.completed_hand is not None
    assert result.completed_hand.status is HandRecordStatus.COMPLETED
    assert result.completed_hand.hand_number == 1
    assert result.completed_hand.ended_in_showdown is True
    assert result.completed_hand.current_public_view.phase.value == "hand_complete"
    assert orchestrator.current_hand_record is None


def test_orchestrator_stores_completed_hands_and_notifies_agents() -> None:
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

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))

    assert len(orchestrator.completed_hand_archives) == 1
    assert len(orchestrator.completed_hands) == 1
    archive = orchestrator.completed_hand_archives[0]
    record = orchestrator.completed_hands[0]
    assert archive.record is record
    assert record.status is HandRecordStatus.COMPLETED
    assert record.hand_number == 1
    assert tuple(event.event_type for event in archive.trace.initial_events) == (
        "hand_started",
        "blind_posted",
        "blind_posted",
        "street_started",
    )
    assert archive.trace.final_state is not None

    assert len(agent_one.completed_hand_records) == 1
    assert len(agent_two.completed_hand_records) == 1
    assert agent_one.completed_hand_records[0][0] is record
    assert agent_two.completed_hand_records[0][0] is record
    assert agent_one.completed_hand_records[0][1].seat_id == "p1"
    assert agent_two.completed_hand_records[0][1].seat_id == "p2"


def test_orchestrator_timeout_falls_back_to_fold_when_check_is_not_legal() -> None:
    agent_one = SlowScriptedAgent(
        "p1",
        actions=[PlayerAction(ActionType.CALL)],
        slow_indices={0},
        delay_seconds=0.05,
        keeps_table_alive=False,
    )
    agent_two = ScriptedAgent("p2", actions=[], keeps_table_alive=False)
    orchestrator = make_heads_up_orchestrator(agent_one, agent_two)
    orchestrator.turn_timeout_seconds = 0.01

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))

    first_action = next(event for event in orchestrator.event_log if event.event_type == "action_applied")
    assert first_action.payload["seat_id"] == "p1"
    assert first_action.payload["action"] == "fold"
    assert orchestrator.current_turn_timer is None


def test_orchestrator_timeout_sits_out_human_agent() -> None:
    agent_one = SlowHumanScriptedAgent(
        "p1",
        actions=[PlayerAction(ActionType.CALL)],
        slow_indices={0},
        delay_seconds=0.05,
        keeps_table_alive=False,
    )
    agent_two = ScriptedAgent("p2", actions=[], keeps_table_alive=False)
    orchestrator = make_heads_up_orchestrator(agent_one, agent_two)
    orchestrator.turn_timeout_seconds = 0.01

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))

    event_types = [event.event_type for event in orchestrator.event_log]
    assert "seat_sat_out" in event_types
    seat_state = next(seat for seat in orchestrator.engine.get_public_table_view().seats if seat.seat_id == "p1")
    assert seat_state.is_sitting_out is True


def test_orchestrator_timeout_falls_back_to_check_when_available() -> None:
    agent_one = SlowScriptedAgent(
        "p1",
        actions=[
            PlayerAction(ActionType.CALL),
            PlayerAction(ActionType.CHECK),
        ],
        slow_indices={1},
        delay_seconds=0.05,
        keeps_table_alive=False,
    )
    agent_two = ScriptedAgent(
        "p2",
        actions=[
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
            PlayerAction(ActionType.CHECK),
        ],
        keeps_table_alive=False,
    )
    orchestrator = make_heads_up_orchestrator(agent_one, agent_two)
    orchestrator.turn_timeout_seconds = 0.01

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))

    flop_action = next(
        event for event in orchestrator.event_log
        if event.event_type == "action_applied"
        and event.payload["seat_id"] == "p1"
        and event.payload["action"] == "check"
    )
    assert flop_action.payload["action"] == "check"
    assert orchestrator.current_turn_timer is None


def test_orchestrator_keeps_same_timer_deadline_after_invalid_action() -> None:
    agent_one = TimerInspectingAgent(
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
    orchestrator.turn_timeout_seconds = 5

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))

    assert len(agent_one.timer_starts) >= 2
    assert agent_one.timer_starts[0] == agent_one.timer_starts[1]


def test_orchestrator_clears_timer_after_pending_turn_resolves() -> None:
    agent_one = SlowScriptedAgent(
        "p1",
        actions=[PlayerAction(ActionType.CALL)],
        slow_indices={0},
        delay_seconds=0.05,
    )
    agent_two = ScriptedAgent("p2", actions=[])
    orchestrator = make_heads_up_orchestrator(agent_one, agent_two)
    orchestrator.turn_timeout_seconds = 0.02

    async def scenario() -> None:
        task = asyncio.create_task(orchestrator.play_hand())
        await asyncio.sleep(0.005)
        assert orchestrator.current_turn_timer is not None
        await task
        assert orchestrator.current_turn_timer is None

    asyncio.run(scenario())


def test_orchestrator_pauses_until_players_sit_back_in() -> None:
    agent_one = ScriptedAgent("p1", actions=[PlayerAction(ActionType.FOLD)], keeps_table_alive=False)
    agent_two = ScriptedAgent("p2", actions=[], keeps_table_alive=False)
    orchestrator = make_heads_up_orchestrator(agent_one, agent_two)

    async def scenario() -> None:
        await orchestrator.sit_out_seat("p2", reason="manual")
        task = asyncio.create_task(orchestrator.play_hand())
        await asyncio.sleep(0.01)
        assert any(event.event_type == "table_paused" for event in orchestrator.event_log)
        assert orchestrator.engine.get_phase() == GamePhase.WAITING_FOR_PLAYERS

        await orchestrator.sit_in_seat("p2", reason="manual")
        result = await task

        assert result.started is True
        event_types = [event.event_type for event in orchestrator.event_log]
        assert "table_resumed" in event_types
        assert "hand_started" in event_types

    asyncio.run(scenario())


def test_orchestrator_uses_shared_fallback_for_agent_errors() -> None:
    engine = PokerEngine.create_table(
        TableConfig(deck_factory=DeckSequenceFactory([("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc")])),
        [SeatConfig("p1", "P1"), SeatConfig("p2", "P2")],
    )
    agent_one = RaisingAgent("p1", exc=RuntimeError("boom"))
    agent_two = ScriptedAgent("p2", actions=[])
    orchestrator = GameOrchestrator(engine, {"p1": agent_one, "p2": agent_two})

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))

    first_action = next(event for event in orchestrator.event_log if event.event_type == "action_applied")
    assert first_action.payload["seat_id"] == "p1"
    assert first_action.payload["action"] == "fold"


def test_orchestrator_completes_table_when_human_turn_goes_idle() -> None:
    agent_one = SlowScriptedAgent(
        "p1",
        actions=[PlayerAction(ActionType.CALL)],
        slow_indices={0},
        delay_seconds=0.05,
        keeps_table_alive=True,
    )
    agent_two = ScriptedAgent("p2", actions=[], keeps_table_alive=False)
    orchestrator = make_heads_up_orchestrator(agent_one, agent_two)
    orchestrator.turn_timeout_seconds = 0.02
    orchestrator.idle_close_seconds = 0.02

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))

    table_completed = [event for event in orchestrator.event_log if event.event_type == "table_completed"]
    assert table_completed
    assert table_completed[-1].payload["reason"] == "idle_timeout"
    assert all(event.event_type != "action_applied" for event in orchestrator.event_log)
    assert any(update.update_type == PlayerUpdateType.TABLE_COMPLETED for update in agent_one.updates)
    assert any(update.update_type == PlayerUpdateType.TABLE_COMPLETED for update in agent_two.updates)


def test_orchestrator_invalid_human_action_does_not_refresh_idle_deadline() -> None:
    agent_one = SlowScriptedAgent(
        "p1",
        actions=[
            PlayerAction(ActionType.BET, amount=300),
            PlayerAction(ActionType.CALL),
        ],
        slow_indices={1},
        delay_seconds=0.05,
        keeps_table_alive=True,
    )
    agent_two = ScriptedAgent("p2", actions=[], keeps_table_alive=False)
    orchestrator = make_heads_up_orchestrator(agent_one, agent_two)
    orchestrator.turn_timeout_seconds = 0.02
    orchestrator.idle_close_seconds = 0.02

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))

    assert len(agent_one.decisions) == 2
    assert agent_one.decisions[1].validation_error is not None
    assert agent_one.decisions[1].validation_error.code == "illegal_action"
    table_completed = [event for event in orchestrator.event_log if event.event_type == "table_completed"]
    assert table_completed[-1].payload["reason"] == "idle_timeout"
    assert all(event.event_type != "action_applied" for event in orchestrator.event_log)


def test_orchestrator_bot_turn_does_not_keep_table_alive_after_human_action() -> None:
    agent_one = ScriptedAgent(
        "p1",
        actions=[PlayerAction(ActionType.CALL)],
        keeps_table_alive=True,
    )
    agent_two = SlowScriptedAgent(
        "p2",
        actions=[PlayerAction(ActionType.CHECK)],
        slow_indices={0},
        delay_seconds=0.05,
        keeps_table_alive=False,
    )
    orchestrator = make_heads_up_orchestrator(agent_one, agent_two)
    orchestrator.turn_timeout_seconds = 0.02
    orchestrator.idle_close_seconds = 0.02

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))

    action_events = [event for event in orchestrator.event_log if event.event_type == "action_applied"]
    assert len(action_events) == 1
    assert action_events[0].payload["seat_id"] == "p1"
    table_completed = [event for event in orchestrator.event_log if event.event_type == "table_completed"]
    assert table_completed[-1].payload["reason"] == "idle_timeout"
