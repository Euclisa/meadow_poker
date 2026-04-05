from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from poker_bot.orchestrator import GameOrchestrator
from poker_bot.players.base import PlayerAgent
from poker_bot.poker.decks import DeckSequenceFactory, decode_card_order, encode_card_order
from poker_bot.poker.engine import PokerEngine
from poker_bot.replay import (
    HandReplayBuildError,
    HandReplaySession,
    ReplayAnalysisError,
    build_replay_decision_spot,
    validate_hand_trace,
)
from poker_bot.types import ActionType, DecisionRequest, PlayerAction, PlayerUpdate, SeatConfig, TableConfig


class ScriptedAgent(PlayerAgent):
    def __init__(self, seat_id: str, actions: list[PlayerAction]) -> None:
        self.seat_id = seat_id
        self._actions = list(actions)
        self.updates: list[PlayerUpdate] = []

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        del decision
        if not self._actions:
            raise AssertionError(f"No scripted action left for {self.seat_id}")
        return self._actions.pop(0)

    async def notify_update(self, update: PlayerUpdate) -> None:
        self.updates.append(update)

    async def close(self) -> None:
        return None


def _make_engine(deck: tuple[str, ...]) -> PokerEngine:
    return PokerEngine.create_table(
        TableConfig(deck_factory=DeckSequenceFactory([deck])),
        [SeatConfig("p1", "P1"), SeatConfig("p2", "P2")],
    )


def test_card_order_codec_round_trip() -> None:
    cards = ("As", "Kh", "Td", "2c")

    encoded = encode_card_order(cards)

    assert encoded == "AsKhTd2c"
    assert decode_card_order(encoded) == cards


def test_engine_can_hydrate_from_hand_state_snapshot() -> None:
    engine = _make_engine(("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"))
    engine.start_next_hand()
    snapshot = engine.export_hand_state_snapshot()

    replay_engine = PokerEngine.from_hand_state_snapshot(snapshot)

    assert replay_engine.get_public_table_view() == engine.get_public_table_view()
    assert replay_engine.get_acting_seat() == engine.get_acting_seat()
    assert replay_engine.get_player_view("p1") == engine.get_player_view("p1")
    assert replay_engine.get_legal_actions("p1") == engine.get_legal_actions("p1")


def test_archived_hand_trace_replays_completed_hand_and_supports_step_back() -> None:
    engine = _make_engine(("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"))
    orchestrator = GameOrchestrator(
        engine,
        {
            "p1": ScriptedAgent(
                "p1",
                [
                    PlayerAction(ActionType.CALL),
                    PlayerAction(ActionType.CHECK),
                    PlayerAction(ActionType.CHECK),
                    PlayerAction(ActionType.CHECK),
                ],
            ),
            "p2": ScriptedAgent(
                "p2",
                [
                    PlayerAction(ActionType.CHECK),
                    PlayerAction(ActionType.CHECK),
                    PlayerAction(ActionType.CHECK),
                    PlayerAction(ActionType.CHECK),
                ],
            ),
        },
    )

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))
    archive = orchestrator.completed_hand_archives[0]
    validate_hand_trace(archive.trace)
    replay_session = HandReplaySession(archive.trace, viewer_seat_id="p1")

    start_frame = replay_session.materialize(0)
    final_frame = replay_session.materialize(archive.trace.total_steps - 1)
    previous_frame = replay_session.step_back()

    assert archive.trace.total_steps == len(archive.trace.transitions) + 1
    assert tuple(event.event_type for event in archive.trace.initial_events) == (
        "hand_started",
        "blind_posted",
        "blind_posted",
        "street_started",
    )
    assert start_frame.public_table_view == archive.record.start_public_view
    assert tuple(event.event_type for event in start_frame.visible_events) == (
        "hand_started",
        "blind_posted",
        "blind_posted",
        "street_started",
    )
    assert final_frame.public_table_view == archive.record.current_public_view
    assert final_frame.player_view is not None
    assert final_frame.player_view.hole_cards == ("As", "Ad")
    assert previous_frame.step_index == archive.trace.total_steps - 2


def test_archived_hand_trace_records_action_and_automatic_steps_separately() -> None:
    engine = _make_engine(("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"))
    orchestrator = GameOrchestrator(
        engine,
        {
            "p1": ScriptedAgent("p1", [PlayerAction(ActionType.FOLD)]),
            "p2": ScriptedAgent("p2", []),
        },
    )

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))
    archive = orchestrator.completed_hand_archives[0]
    replay_session = HandReplaySession(archive.trace, viewer_seat_id="p1")

    assert [transition.kind for transition in archive.trace.transitions] == ["action", "automatic"]

    action_frame = replay_session.materialize(1)
    final_frame = replay_session.materialize(2)

    assert action_frame.public_table_view.phase.value == "preflop"
    assert action_frame.public_table_view.acting_seat_id is None
    assert final_frame.public_table_view.phase.value == "hand_complete"


def test_build_replay_decision_spot_reconstructs_viewer_action_context() -> None:
    engine = _make_engine(("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"))
    orchestrator = GameOrchestrator(
        engine,
        {
            "p1": ScriptedAgent("p1", [PlayerAction(ActionType.FOLD)]),
            "p2": ScriptedAgent("p2", []),
        },
    )

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))
    archive = orchestrator.completed_hand_archives[0]

    spot = build_replay_decision_spot(
        archive.trace,
        step_index=0,
        viewer_seat_id="p1",
    )

    assert spot.frame.step_index == 0
    assert spot.decision.player_view.hole_cards == ("As", "Ad")
    assert {action.action_type.value for action in spot.decision.legal_actions} == {"fold", "call", "raise"}
    assert spot.next_transition.action is not None
    assert spot.next_transition.action.action_type is ActionType.FOLD


def test_build_replay_decision_spot_rejects_other_player_automatic_and_final_steps() -> None:
    engine = _make_engine(("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"))
    orchestrator = GameOrchestrator(
        engine,
        {
            "p1": ScriptedAgent(
                "p1",
                [
                    PlayerAction(ActionType.CALL),
                    PlayerAction(ActionType.CHECK),
                    PlayerAction(ActionType.CHECK),
                    PlayerAction(ActionType.CHECK),
                ],
            ),
            "p2": ScriptedAgent(
                "p2",
                [
                    PlayerAction(ActionType.CHECK),
                    PlayerAction(ActionType.CHECK),
                    PlayerAction(ActionType.CHECK),
                    PlayerAction(ActionType.CHECK),
                ],
            ),
        },
    )

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))
    archive = orchestrator.completed_hand_archives[0]

    with pytest.raises(ReplayAnalysisError):
        build_replay_decision_spot(archive.trace, step_index=1, viewer_seat_id="p1")

    fold_engine = _make_engine(("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"))
    fold_orchestrator = GameOrchestrator(
        fold_engine,
        {
            "p1": ScriptedAgent("p1", [PlayerAction(ActionType.FOLD)]),
            "p2": ScriptedAgent("p2", []),
        },
    )
    asyncio.run(fold_orchestrator.run(max_hands=1, close_agents=False))
    fold_archive = fold_orchestrator.completed_hand_archives[0]

    with pytest.raises(ReplayAnalysisError):
        build_replay_decision_spot(fold_archive.trace, step_index=1, viewer_seat_id="p1")
    with pytest.raises(ReplayAnalysisError):
        build_replay_decision_spot(
            fold_archive.trace,
            step_index=fold_archive.trace.total_steps - 1,
            viewer_seat_id="p1",
        )


def test_validate_hand_trace_fails_loudly_for_inconsistent_final_state() -> None:
    engine = _make_engine(("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"))
    orchestrator = GameOrchestrator(
        engine,
        {
            "p1": ScriptedAgent("p1", [PlayerAction(ActionType.FOLD)]),
            "p2": ScriptedAgent("p2", []),
        },
    )

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))
    archive = orchestrator.completed_hand_archives[0]
    assert archive.trace.final_state is not None
    tampered_trace = replace(
        archive.trace,
        final_state=replace(
            archive.trace.final_state,
            current_bet=999,
        ),
    )

    with pytest.raises(HandReplayBuildError):
        validate_hand_trace(tampered_trace)
