from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from poker_bot.orchestrator import GameOrchestrator
from poker_bot.players.base import PlayerAgent
from poker_bot.poker.decks import DeckSequenceFactory, decode_card_order, encode_card_order
from poker_bot.poker.engine import PokerEngine
from poker_bot.replay import HandReplayBuildError, HandReplaySession, build_hand_replay_record
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


def test_engine_can_hydrate_from_replay_seed() -> None:
    engine = _make_engine(("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"))
    engine.start_next_hand()
    seed = engine.export_hand_replay_seed()
    deck_order = engine.export_remaining_deck_order()

    replay_engine = PokerEngine.from_hand_replay_seed(seed, deck_order)

    assert replay_engine.get_public_table_view() == engine.get_public_table_view()
    assert replay_engine.get_acting_seat() == engine.get_acting_seat()
    assert replay_engine.get_player_view("p1") == engine.get_player_view("p1")
    assert replay_engine.get_legal_actions("p1") == engine.get_legal_actions("p1")


def test_build_hand_replay_record_replays_completed_hand_and_supports_step_back() -> None:
    engine = _make_engine(("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"))
    orchestrator = GameOrchestrator(
        engine,
        {
            "p1": ScriptedAgent("p1", [PlayerAction(ActionType.CALL), PlayerAction(ActionType.CHECK), PlayerAction(ActionType.CHECK), PlayerAction(ActionType.CHECK)]),
            "p2": ScriptedAgent("p2", [PlayerAction(ActionType.CHECK), PlayerAction(ActionType.CHECK), PlayerAction(ActionType.CHECK), PlayerAction(ActionType.CHECK)]),
        },
    )

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))
    record = orchestrator.completed_hands[0]
    replay_record = build_hand_replay_record(record)
    replay_session = HandReplaySession(replay_record, viewer_seat_id="p1")

    start_frame = replay_session.materialize(0)
    final_frame = replay_session.materialize(replay_record.total_steps - 1)
    previous_frame = replay_session.step_back()

    assert replay_record.total_steps == len(replay_record.actions) + 1
    assert start_frame.public_table_view == record.start_public_view
    assert final_frame.public_table_view == record.current_public_view
    assert final_frame.player_view is not None
    assert final_frame.player_view.hole_cards == ("As", "Ad")
    assert previous_frame.step_index == replay_record.total_steps - 2


def test_build_hand_replay_record_fails_loudly_for_inconsistent_hand() -> None:
    engine = _make_engine(("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"))
    orchestrator = GameOrchestrator(
        engine,
        {
            "p1": ScriptedAgent("p1", [PlayerAction(ActionType.FOLD)]),
            "p2": ScriptedAgent("p2", []),
        },
    )

    asyncio.run(orchestrator.run(max_hands=1, close_agents=False))
    record = orchestrator.completed_hands[0]
    tampered = replace(
        record,
        current_public_view=replace(
            record.current_public_view,
            pot_total=999,
        ),
    )

    with pytest.raises(HandReplayBuildError):
        build_hand_replay_record(tampered)
