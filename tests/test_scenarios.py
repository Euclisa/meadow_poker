from __future__ import annotations

import asyncio
from dataclasses import dataclass

from meadow.orchestrator import GameOrchestrator
from meadow.players.base import PlayerAgent
from meadow.poker.decks import DeckSequenceFactory
from meadow.poker.engine import PokerEngine
from meadow.types import ActionType, DecisionRequest, PlayerAction, PlayerUpdate, SeatConfig, TableConfig


class ScriptedAgent(PlayerAgent):
    def __init__(self, seat_id: str, actions: list[PlayerAction]) -> None:
        self.seat_id = seat_id
        self._actions = list(actions)
        self.decisions: list[DecisionRequest] = []
        self.updates: list[PlayerUpdate] = []

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        self.decisions.append(decision)
        if not self._actions:
            raise AssertionError(f"No scripted action left for {self.seat_id}")
        return self._actions.pop(0)

    async def notify_update(self, update: PlayerUpdate) -> None:
        self.updates.append(update)

    async def close(self) -> None:
        return None


@dataclass(frozen=True, slots=True)
class Scenario:
    name: str
    hands: tuple[tuple[str, ...], ...]
    stacks: tuple[int, ...]
    scripted_actions: dict[str, tuple[PlayerAction, ...]]
    expected_stacks: dict[str, int]
    expected_event_types: tuple[str, ...] = ()
    small_blind: int = 50
    big_blind: int = 100


def run_scenario(scenario: Scenario, *, max_hands: int | None = None) -> tuple[GameOrchestrator, dict[str, ScriptedAgent]]:
    seats = [
        SeatConfig(seat_id=f"p{index + 1}", name=f"P{index + 1}", starting_stack=stack)
        for index, stack in enumerate(scenario.stacks)
    ]
    engine = PokerEngine.create_table(
        TableConfig(
            small_blind=scenario.small_blind,
            big_blind=scenario.big_blind,
            deck_factory=DeckSequenceFactory(list(scenario.hands)),
        ),
        seats,
    )
    agents = {
        seat.seat_id: ScriptedAgent(seat.seat_id, list(scenario.scripted_actions.get(seat.seat_id, ())))
        for seat in seats
    }
    orchestrator = GameOrchestrator(engine, agents)
    asyncio.run(orchestrator.run(max_hands=max_hands, close_agents=False))
    return orchestrator, agents


def test_scenario_fold_preflop_awards_blinds() -> None:
    scenario = Scenario(
        name="fold_preflop",
        hands=(("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),),
        stacks=(2_000, 2_000),
        scripted_actions={"p1": (PlayerAction(ActionType.FOLD),)},
        expected_stacks={"p1": 1_950, "p2": 2_050},
        expected_event_types=("hand_awarded", "hand_completed"),
    )

    orchestrator, _agents = run_scenario(scenario, max_hands=1)

    final_stacks = {seat.seat_id: seat.stack for seat in orchestrator.engine.get_public_table_view().seats}
    event_types = tuple(event.event_type for event in orchestrator.event_log)
    assert final_stacks == scenario.expected_stacks
    for event_type in scenario.expected_event_types:
        assert event_type in event_types


def test_scenario_heads_up_checkdown_showdown() -> None:
    scenario = Scenario(
        name="checkdown_showdown",
        hands=(("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),),
        stacks=(2_000, 2_000),
        scripted_actions={
            "p1": (
                PlayerAction(ActionType.CALL),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
            ),
            "p2": (
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
            ),
        },
        expected_stacks={"p1": 2_100, "p2": 1_900},
        expected_event_types=("showdown_started", "pot_awarded", "hand_completed"),
    )

    orchestrator, _agents = run_scenario(scenario, max_hands=1)

    final_stacks = {seat.seat_id: seat.stack for seat in orchestrator.engine.get_public_table_view().seats}
    event_types = tuple(event.event_type for event in orchestrator.event_log)
    assert final_stacks == scenario.expected_stacks
    for event_type in scenario.expected_event_types:
        assert event_type in event_types


def test_scenario_split_pot_returns_even_stacks() -> None:
    scenario = Scenario(
        name="split_pot",
        hands=(("As", "Qh", "Kd", "Jc", "2c", "3d", "4h", "5s", "6c"),),
        stacks=(2_000, 2_000),
        scripted_actions={
            "p1": (
                PlayerAction(ActionType.CALL),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
            ),
            "p2": (
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
            ),
        },
        expected_stacks={"p1": 2_000, "p2": 2_000},
        expected_event_types=("showdown_started", "pot_awarded"),
    )

    orchestrator, _agents = run_scenario(scenario, max_hands=1)

    final_stacks = {seat.seat_id: seat.stack for seat in orchestrator.engine.get_public_table_view().seats}
    pot_awards = [event.payload for event in orchestrator.event_log if event.event_type == "pot_awarded"]
    assert final_stacks == scenario.expected_stacks
    assert len(pot_awards) == 2
    assert {payload["amount"] for payload in pot_awards} == {100}


def test_scenario_multiway_side_pot_catalog() -> None:
    scenario = Scenario(
        name="side_pot",
        hands=(("Qc", "2h", "As", "Qd", "2s", "Kd", "2c", "7d", "8h", "9s", "Tc"),),
        stacks=(200, 500, 1_000),
        scripted_actions={
            "p1": (PlayerAction(ActionType.RAISE, amount=200),),
            "p2": (
                PlayerAction(ActionType.CALL),
                PlayerAction(ActionType.CALL),
            ),
            "p3": (PlayerAction(ActionType.RAISE, amount=500),),
        },
        expected_stacks={"p1": 0, "p2": 1_200, "p3": 500},
        expected_event_types=("showdown_started", "pot_awarded"),
    )

    orchestrator, _agents = run_scenario(scenario, max_hands=1)

    final_stacks = {seat.seat_id: seat.stack for seat in orchestrator.engine.get_public_table_view().seats}
    assert final_stacks == scenario.expected_stacks


def test_scenario_factory_exhaustion_stops_session_smoothly() -> None:
    scenario = Scenario(
        name="finite_scripted_hands",
        hands=(("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),),
        stacks=(2_000, 2_000),
        scripted_actions={"p1": (PlayerAction(ActionType.FOLD),)},
        expected_stacks={"p1": 1_950, "p2": 2_050},
        expected_event_types=("table_completed",),
    )

    orchestrator, agents = run_scenario(scenario, max_hands=2)

    final_stacks = {seat.seat_id: seat.stack for seat in orchestrator.engine.get_public_table_view().seats}
    event_types = tuple(event.event_type for event in orchestrator.event_log)
    terminal_events = {
        event.event_type
        for agent in agents.values()
        for update in agent.updates
        for event in update.events
    }
    assert final_stacks == scenario.expected_stacks
    assert orchestrator.engine.get_phase().value == "table_complete"
    assert "table_completed" in event_types
    assert "table_completed" in terminal_events


def test_scenario_all_in_auto_runout_needs_no_postflop_decisions() -> None:
    scenario = Scenario(
        name="all_in_auto_runout",
        hands=(("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),),
        stacks=(200, 2_000),
        scripted_actions={
            "p1": (PlayerAction(ActionType.RAISE, amount=200),),
            "p2": (PlayerAction(ActionType.CALL),),
        },
        expected_stacks={"p1": 400, "p2": 1_800},
        expected_event_types=("showdown_started", "pot_awarded", "hand_completed"),
    )

    orchestrator, agents = run_scenario(scenario, max_hands=1)

    final_stacks = {seat.seat_id: seat.stack for seat in orchestrator.engine.get_public_table_view().seats}
    event_types = [event.event_type for event in orchestrator.event_log]
    street_started_count = event_types.count("street_started")
    assert final_stacks == scenario.expected_stacks
    assert street_started_count == 4
    assert len(agents["p1"].decisions) == 1
    assert len(agents["p2"].decisions) == 1
    assert "showdown_started" in event_types


def test_scenario_dealer_rotates_across_scripted_hands() -> None:
    scenario = Scenario(
        name="dealer_rotation",
        hands=(
            ("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),
            ("Qs", "Jh", "Qd", "Jd", "2h", "3c", "4d", "5s", "6c"),
        ),
        stacks=(2_000, 2_000),
        scripted_actions={
            "p1": (PlayerAction(ActionType.FOLD),),
            "p2": (PlayerAction(ActionType.FOLD),),
        },
        expected_stacks={"p1": 2_000, "p2": 2_000},
    )

    orchestrator, _agents = run_scenario(scenario, max_hands=2)

    dealer_sequence = [
        event.payload["dealer_seat_id"]
        for event in orchestrator.event_log
        if event.event_type == "hand_started"
    ]
    final_stacks = {seat.seat_id: seat.stack for seat in orchestrator.engine.get_public_table_view().seats}
    assert dealer_sequence == ["p1", "p2"]
    assert final_stacks == scenario.expected_stacks


def test_scenario_bustout_ends_session_when_not_enough_players_remain() -> None:
    scenario = Scenario(
        name="bustout_table_complete",
        hands=(("2c", "As", "3d", "Ah", "Kc", "Qh", "9d", "7c", "4s"),),
        stacks=(50, 2_000),
        scripted_actions={},
        expected_stacks={"p1": 0, "p2": 2_050},
        expected_event_types=("showdown_started", "table_completed"),
    )

    orchestrator, agents = run_scenario(scenario, max_hands=2)

    final_stacks = {seat.seat_id: seat.stack for seat in orchestrator.engine.get_public_table_view().seats}
    table_completed = [event for event in orchestrator.event_log if event.event_type == "table_completed"]
    assert final_stacks == scenario.expected_stacks
    assert orchestrator.engine.get_phase().value == "table_complete"
    assert table_completed[-1].payload["reason"] == "not_enough_players"
    assert len(agents["p1"].decisions) == 0
    assert len(agents["p2"].decisions) == 0


def test_scenario_mid_hand_deck_exhaustion_completes_table_cleanly() -> None:
    scenario = Scenario(
        name="mid_hand_deck_exhaustion",
        hands=(("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s"),),
        stacks=(200, 2_000),
        scripted_actions={
            "p1": (PlayerAction(ActionType.RAISE, amount=200),),
            "p2": (PlayerAction(ActionType.CALL),),
        },
        expected_stacks={"p1": 200, "p2": 2_000},
        expected_event_types=("table_completed", "chips_refunded"),
    )

    orchestrator, agents = run_scenario(scenario, max_hands=1)

    final_stacks = {seat.seat_id: seat.stack for seat in orchestrator.engine.get_public_table_view().seats}
    table_completed = [event for event in orchestrator.event_log if event.event_type == "table_completed"]
    refunds = [event for event in orchestrator.event_log if event.event_type == "chips_refunded"]
    assert final_stacks == scenario.expected_stacks
    assert orchestrator.engine.get_phase().value == "table_complete"
    assert table_completed[-1].payload["reason"] == "deck_exhausted"
    assert len(refunds) == 2
    assert len(agents["p1"].decisions) == 1
    assert len(agents["p2"].decisions) == 1


def test_scenario_odd_chip_goes_to_position_order() -> None:
    scenario = Scenario(
        name="odd_chip_split",
        hands=(("As", "Ad", "Kc", "2h", "3h", "Qc", "7s", "7d", "9c", "Ts", "Jd"),),
        stacks=(10, 10, 10),
        scripted_actions={
            "p1": (
                PlayerAction(ActionType.CALL),
                PlayerAction(ActionType.BET, amount=1),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
            ),
            "p2": (
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CALL),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
            ),
            "p3": (
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.FOLD),
            ),
        },
        expected_stacks={"p1": 10, "p2": 11, "p3": 9},
        expected_event_types=("pot_awarded",),
        small_blind=1,
        big_blind=1,
    )

    orchestrator, _agents = run_scenario(scenario, max_hands=1)

    final_stacks = {seat.seat_id: seat.stack for seat in orchestrator.engine.get_public_table_view().seats}
    pot_awards = [event.payload for event in orchestrator.event_log if event.event_type == "pot_awarded"]
    assert final_stacks == scenario.expected_stacks
    assert pot_awards == [
        {"seat_id": "p1", "amount": 2},
        {"seat_id": "p2", "amount": 3},
    ]


def test_scenario_four_player_side_pots_can_have_different_winners() -> None:
    scenario = Scenario(
        name="four_player_side_pots",
        hands=(("As", "Ks", "Js", "Qs", "Ac", "Kc", "Jc", "Qc", "2h", "7d", "9c", "3s", "4h"),),
        stacks=(100, 300, 500, 1_000),
        scripted_actions={
            "p2": (
                PlayerAction(ActionType.CALL),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CALL),
            ),
            "p3": (
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.BET, amount=400),
            ),
            "p4": (
                PlayerAction(ActionType.CALL),
                PlayerAction(ActionType.CALL),
            ),
            "p1": (PlayerAction(ActionType.CALL),),
        },
        expected_stacks={"p1": 400, "p2": 600, "p3": 0, "p4": 900},
        expected_event_types=("pot_awarded", "showdown_started"),
    )

    orchestrator, _agents = run_scenario(scenario, max_hands=1)

    final_stacks = {seat.seat_id: seat.stack for seat in orchestrator.engine.get_public_table_view().seats}
    pot_awards = [event.payload for event in orchestrator.event_log if event.event_type == "pot_awarded"]
    assert final_stacks == scenario.expected_stacks
    assert pot_awards == [
        {"seat_id": "p1", "amount": 400},
        {"seat_id": "p2", "amount": 600},
        {"seat_id": "p4", "amount": 400},
    ]


def test_scenario_folded_dead_chips_stay_in_pot_but_folder_cannot_win() -> None:
    scenario = Scenario(
        name="dead_chips_side_pot",
        hands=(("As", "Qh", "Kd", "Ac", "Jd", "Kc", "2h", "7d", "9c", "3s", "4h"),),
        stacks=(200, 500, 500),
        scripted_actions={
            "p1": (PlayerAction(ActionType.RAISE, amount=200),),
            "p2": (
                PlayerAction(ActionType.CALL),
                PlayerAction(ActionType.BET, amount=100),
                PlayerAction(ActionType.FOLD),
            ),
            "p3": (
                PlayerAction(ActionType.CALL),
                PlayerAction(ActionType.RAISE, amount=300),
            ),
        },
        expected_stacks={"p1": 600, "p2": 200, "p3": 400},
        expected_event_types=("pot_awarded",),
    )

    orchestrator, _agents = run_scenario(scenario, max_hands=1)

    final_stacks = {seat.seat_id: seat.stack for seat in orchestrator.engine.get_public_table_view().seats}
    pot_awards = [event.payload for event in orchestrator.event_log if event.event_type == "pot_awarded"]
    assert final_stacks == scenario.expected_stacks
    assert pot_awards == [
        {"seat_id": "p1", "amount": 600},
        {"seat_id": "p3", "amount": 400},
    ]


def test_scenario_same_pair_showdown_uses_kicker_tiebreak() -> None:
    scenario = Scenario(
        name="pair_kicker_tiebreak",
        hands=(("As", "Ah", "Kd", "Qd", "Ac", "7s", "6d", "2c", "3h"),),
        stacks=(2_000, 2_000),
        scripted_actions={
            "p1": (
                PlayerAction(ActionType.CALL),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
            ),
            "p2": (
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
                PlayerAction(ActionType.CHECK),
            ),
        },
        expected_stacks={"p1": 2_100, "p2": 1_900},
        expected_event_types=("showdown_started", "pot_awarded"),
    )

    orchestrator, _agents = run_scenario(scenario, max_hands=1)

    final_stacks = {seat.seat_id: seat.stack for seat in orchestrator.engine.get_public_table_view().seats}
    pot_awards = [event.payload for event in orchestrator.event_log if event.event_type == "pot_awarded"]
    assert final_stacks == scenario.expected_stacks
    assert pot_awards == [{"seat_id": "p1", "amount": 200}]


def test_scenario_unlimited_hands_plays_until_last_player_standing() -> None:
    # Hand 1: p1 is dealer/SB(50), p2 is BB(100). p1 folds. Stacks: 50, 2050.
    # Hand 2: p2 is dealer/SB(50), p1 is BB(50 all-in). Auto-runout. p2 wins.
    #         Stacks: 0, 2100.
    # Hand 3 attempt: only p2 funded -> table_complete.
    scenario = Scenario(
        name="last_player_standing",
        hands=(
            ("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),
            ("2h", "As", "7d", "Ad", "Kc", "Qc", "9c", "3s", "4s"),
        ),
        stacks=(100, 2_000),
        scripted_actions={
            "p1": (PlayerAction(ActionType.FOLD),),
        },
        expected_stacks={"p1": 0, "p2": 2_100},
        expected_event_types=("table_completed",),
    )

    orchestrator, _agents = run_scenario(scenario, max_hands=None)

    final_stacks = {seat.seat_id: seat.stack for seat in orchestrator.engine.get_public_table_view().seats}
    funded = [sid for sid, stack in final_stacks.items() if stack > 0]
    table_completed = [e for e in orchestrator.event_log if e.event_type == "table_completed"]
    assert final_stacks == scenario.expected_stacks
    assert len(funded) == 1
    assert table_completed[-1].payload["reason"] == "not_enough_players"


def test_scenario_max_hands_emits_table_completed_event() -> None:
    scenario = Scenario(
        name="max_hands_event",
        hands=(
            ("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),
        ),
        stacks=(2_000, 2_000),
        scripted_actions={"p1": (PlayerAction(ActionType.FOLD),)},
        expected_stacks={"p1": 1_950, "p2": 2_050},
        expected_event_types=("table_completed",),
    )

    orchestrator, _agents = run_scenario(scenario, max_hands=1)

    event_types = [e.event_type for e in orchestrator.event_log]
    table_completed = [e for e in orchestrator.event_log if e.event_type == "table_completed"]
    assert "table_completed" in event_types
    assert table_completed[-1].payload["reason"] == "max_hands_reached"
