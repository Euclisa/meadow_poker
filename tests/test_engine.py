import pytest

from poker_bot.poker.cards import best_hand_details, best_hand_rank, rank_five_cards
from poker_bot.poker.decks import DeckSequenceFactory
from poker_bot.poker.engine import PokerEngine
from poker_bot.types import ActionType, GamePhase, PlayerAction, SeatConfig, TableConfig


def make_engine(
    *,
    deck: tuple[str, ...],
    stacks: tuple[int, ...],
    small_blind: int = 50,
    big_blind: int = 100,
    ante: int = 0,
) -> PokerEngine:
    seats = [
        SeatConfig(seat_id=f"p{index + 1}", name=f"P{index + 1}", starting_stack=stack)
        for index, stack in enumerate(stacks)
    ]
    return PokerEngine.create_table(
        TableConfig(
            small_blind=small_blind,
            big_blind=big_blind,
            ante=ante,
            deck_factory=DeckSequenceFactory([deck]),
        ),
        seats,
    )


def test_best_hand_rank_handles_wheel_straight() -> None:
    assert best_hand_rank(("As", "2d", "3c", "4h", "5s")) == (4, 5)


def test_best_hand_details_returns_label_for_best_combo() -> None:
    rank, label = best_hand_details(("As", "Ah", "Kd", "Kc", "2s", "2d", "Ac"))

    assert rank == (6, 14, 13)
    assert label == "full house, aces full of kings"


def test_start_next_hand_sets_first_actor_and_legal_actions() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Qd", "Ad", "Ks", "Qc", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(2_000, 2_000, 2_000),
    )

    result = engine.start_next_hand()

    assert result.ok is True
    assert engine.get_phase().value == "preflop"
    assert engine.get_acting_seat() == "p1"
    legal_actions = {item.action_type for item in engine.get_legal_actions("p1")}
    assert legal_actions == {ActionType.FOLD, ActionType.CALL, ActionType.RAISE}


def test_table_config_accepts_non_negative_ante_and_rejects_negative() -> None:
    assert TableConfig(ante=0).ante == 0
    assert TableConfig(ante=25).ante == 25
    with pytest.raises(ValueError, match="ante must be non-negative"):
        TableConfig(ante=-1)


def test_start_next_hand_posts_antes_before_blinds_without_affecting_to_call() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Qd", "Ad", "Ks", "Qc", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(2_000, 2_000, 2_000),
        ante=10,
    )

    result = engine.start_next_hand()

    assert result.ok is True
    assert [event.event_type for event in result.events[:6]] == [
        "hand_started",
        "ante_posted",
        "ante_posted",
        "ante_posted",
        "blind_posted",
        "blind_posted",
    ]
    public_view = engine.get_public_table_view()
    p1_view = engine.get_player_view("p1")
    assert public_view.ante == 10
    assert public_view.pot_total == 180
    assert public_view.current_bet == 100
    assert p1_view.to_call == 100
    seat_map = {seat.seat_id: seat for seat in public_view.seats}
    assert seat_map["p1"].contribution == 10
    assert seat_map["p1"].street_contribution == 0
    assert seat_map["p2"].contribution == 60
    assert seat_map["p2"].street_contribution == 50
    assert seat_map["p3"].contribution == 110
    assert seat_map["p3"].street_contribution == 100


def test_invalid_action_does_not_mutate_state() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Ad", "Ks", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(2_000, 2_000),
    )
    engine.start_next_hand()
    before = engine.get_public_table_view()

    result = engine.apply_action("p1", PlayerAction(ActionType.BET, amount=300))

    after = engine.get_public_table_view()
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "illegal_action"
    assert before == after


def test_side_pot_showdown_pays_correctly() -> None:
    engine = make_engine(
        deck=(
            "Qc",
            "2h",
            "As",
            "Qd",
            "2s",
            "Kd",
            "2c",
            "7d",
            "8h",
            "9s",
            "Tc",
        ),
        stacks=(200, 500, 1_000),
    )
    engine.start_next_hand()

    result = engine.apply_action("p1", PlayerAction(ActionType.RAISE, amount=200))
    assert result.ok is True
    result = engine.apply_action("p2", PlayerAction(ActionType.CALL))
    assert result.ok is True
    result = engine.apply_action("p3", PlayerAction(ActionType.RAISE, amount=500))
    assert result.ok is True
    result = engine.apply_action("p2", PlayerAction(ActionType.CALL))

    assert result.ok is True
    assert engine.is_hand_complete() is True

    public_view = engine.get_public_table_view()
    stacks = {seat.seat_id: seat.stack for seat in public_view.seats}
    assert stacks == {"p1": 0, "p2": 1_200, "p3": 500}


def test_showdown_emits_revealed_hole_cards_before_payouts() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(2_000, 2_000),
    )
    engine.start_next_hand()

    result = engine.apply_action("p1", PlayerAction(ActionType.CALL))
    assert result.ok is True
    result = engine.apply_action("p2", PlayerAction(ActionType.CHECK))
    assert result.ok is True
    result = engine.apply_action("p2", PlayerAction(ActionType.CHECK))
    assert result.ok is True
    result = engine.apply_action("p1", PlayerAction(ActionType.CHECK))
    assert result.ok is True
    result = engine.apply_action("p2", PlayerAction(ActionType.CHECK))
    assert result.ok is True
    result = engine.apply_action("p1", PlayerAction(ActionType.CHECK))
    assert result.ok is True
    result = engine.apply_action("p2", PlayerAction(ActionType.CHECK))
    assert result.ok is True
    result = engine.apply_action("p1", PlayerAction(ActionType.CHECK))

    assert result.ok is True
    event_types = [event.event_type for event in result.events]
    assert event_types == [
        "action_applied",
        "showdown_started",
        "showdown_revealed",
        "showdown_revealed",
        "pot_awarded",
        "hand_completed",
    ]
    reveal_payloads = [event.payload for event in result.events if event.event_type == "showdown_revealed"]
    assert reveal_payloads == [
        {"seat_id": "p1", "hole_cards": ("As", "Ad"), "hand_label": "one pair, aces"},
        {"seat_id": "p2", "hole_cards": ("Kh", "Kd"), "hand_label": "one pair, kings"},
    ]


def test_showdown_reveals_only_live_non_folded_players() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Qd", "Ad", "Ks", "Qc", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(2_000, 2_000, 2_000),
    )
    engine.start_next_hand()

    result = engine.apply_action("p1", PlayerAction(ActionType.FOLD))
    assert result.ok is True
    result = engine.apply_action("p2", PlayerAction(ActionType.CALL))
    assert result.ok is True
    result = engine.apply_action("p3", PlayerAction(ActionType.CHECK))
    assert result.ok is True
    result = engine.apply_action("p2", PlayerAction(ActionType.CHECK))
    assert result.ok is True
    result = engine.apply_action("p3", PlayerAction(ActionType.CHECK))
    assert result.ok is True
    result = engine.apply_action("p2", PlayerAction(ActionType.CHECK))
    assert result.ok is True
    result = engine.apply_action("p3", PlayerAction(ActionType.CHECK))
    assert result.ok is True
    result = engine.apply_action("p2", PlayerAction(ActionType.CHECK))
    assert result.ok is True
    result = engine.apply_action("p3", PlayerAction(ActionType.CHECK))

    assert result.ok is True
    reveal_seat_ids = [event.payload["seat_id"] for event in result.events if event.event_type == "showdown_revealed"]
    assert reveal_seat_ids == ["p2", "p3"]


def test_scripted_deck_factory_ends_table_cleanly_when_out_of_hands() -> None:
    engine = PokerEngine.create_table(
        TableConfig(
            deck_factory=DeckSequenceFactory(
                [
                    ("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),
                ]
            )
        ),
        [SeatConfig("p1", "P1"), SeatConfig("p2", "P2")],
    )

    first_hand = engine.start_next_hand()
    hand_end = engine.apply_action("p1", PlayerAction(ActionType.FOLD))
    second_hand = engine.start_next_hand()

    assert first_hand.ok is True
    assert hand_end.ok is True
    assert second_hand.ok is False
    assert second_hand.error is not None
    assert second_hand.error.code == "no_more_hands"
    assert engine.get_phase().value == "table_complete"
    assert second_hand.events[-1].event_type == "table_completed"


def test_short_all_in_raise_does_not_reopen_action() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(1_000, 250),
    )
    engine.start_next_hand()

    assert engine.apply_action("p1", PlayerAction(ActionType.RAISE, amount=200)).ok is True
    assert engine.apply_action("p2", PlayerAction(ActionType.RAISE, amount=250)).ok is True

    legal_actions = {item.action_type for item in engine.get_legal_actions("p1")}
    assert legal_actions == {ActionType.FOLD, ActionType.CALL}


def test_full_raise_reopens_action() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(1_000, 400),
    )
    engine.start_next_hand()

    assert engine.apply_action("p1", PlayerAction(ActionType.RAISE, amount=200)).ok is True
    assert engine.apply_action("p2", PlayerAction(ActionType.RAISE, amount=400)).ok is True

    legal_actions = {item.action_type for item in engine.get_legal_actions("p1")}
    raise_action = next(item for item in engine.get_legal_actions("p1") if item.action_type == ActionType.RAISE)
    assert legal_actions == {ActionType.FOLD, ActionType.CALL, ActionType.RAISE}
    assert raise_action.min_amount == 600
    assert raise_action.max_amount == 1_000


def test_raise_below_minimum_is_rejected_but_exact_minimum_is_allowed() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(1_000, 1_000),
    )
    engine.start_next_hand()

    too_small = engine.apply_action("p1", PlayerAction(ActionType.RAISE, amount=199))
    exact_minimum = engine.apply_action("p1", PlayerAction(ActionType.RAISE, amount=200))

    assert too_small.ok is False
    assert too_small.error is not None
    assert too_small.error.code == "amount_too_small"
    assert exact_minimum.ok is True


def test_seat_snapshot_exposes_street_contribution_that_resets_on_new_street() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(2_000, 2_000),
    )
    engine.start_next_hand()

    preflop_view = engine.get_public_table_view()
    preflop_seats = {seat.seat_id: seat for seat in preflop_view.seats}
    assert preflop_seats["p1"].contribution == 50
    assert preflop_seats["p1"].street_contribution == 50
    assert preflop_seats["p2"].contribution == 100
    assert preflop_seats["p2"].street_contribution == 100

    assert engine.apply_action("p1", PlayerAction(ActionType.CALL)).ok is True
    flop_entry = engine.apply_action("p2", PlayerAction(ActionType.CHECK))
    assert flop_entry.ok is True
    assert any(event.event_type == "street_started" and event.payload["phase"] == "flop" for event in flop_entry.events)

    flop_view = engine.get_public_table_view()
    flop_seats = {seat.seat_id: seat for seat in flop_view.seats}
    assert flop_seats["p1"].contribution == 100
    assert flop_seats["p1"].street_contribution == 0
    assert flop_seats["p2"].contribution == 100
    assert flop_seats["p2"].street_contribution == 0


def test_partial_ante_posts_remaining_stack() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Qd", "Ad", "Ks", "Qc", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(15, 2_000),
        ante=20,
    )

    result = engine.start_next_hand()

    assert result.ok is True
    ante_events = [event for event in result.events if event.event_type == "ante_posted"]
    assert [event.payload["amount"] for event in ante_events] == [15, 20]


def test_player_all_in_from_ante_stays_in_hand_and_is_dealt_cards() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Qd", "Ad", "Ks", "Qc", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(5, 2_000, 2_000),
        ante=5,
    )

    result = engine.start_next_hand()

    assert result.ok is True
    p1 = next(seat for seat in engine.get_public_table_view().seats if seat.seat_id == "p1")
    assert p1.in_hand is True
    assert p1.all_in is True
    assert engine.get_player_view("p1").hole_cards == ("As", "Ad")
    assert engine.get_acting_seat() == "p2"


def test_short_stack_all_in_raise_below_full_minimum_is_still_legal() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(180, 1_000),
    )
    engine.start_next_hand()

    legal_actions = engine.get_legal_actions("p1")
    raise_action = next(item for item in legal_actions if item.action_type == ActionType.RAISE)
    result = engine.apply_action("p1", PlayerAction(ActionType.RAISE, amount=180))

    assert raise_action.min_amount == 180
    assert raise_action.max_amount == 180
    assert result.ok is True


def test_invalid_action_matrix_returns_specific_codes_without_mutation() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(2_000, 2_000),
    )
    engine.start_next_hand()
    before = engine.get_public_table_view()

    wrong_actor = engine.apply_action("p2", PlayerAction(ActionType.CALL))
    missing_amount = engine.apply_action("p1", PlayerAction(ActionType.RAISE))
    unexpected_amount = engine.apply_action("p1", PlayerAction(ActionType.CALL, amount=100))
    amount_too_large = engine.apply_action("p1", PlayerAction(ActionType.RAISE, amount=5_000))

    after = engine.get_public_table_view()
    assert wrong_actor.ok is False and wrong_actor.error is not None
    assert missing_amount.ok is False and missing_amount.error is not None
    assert unexpected_amount.ok is False and unexpected_amount.error is not None
    assert amount_too_large.ok is False and amount_too_large.error is not None
    assert wrong_actor.error.code == "not_your_turn"
    assert missing_amount.error.code == "missing_amount"
    assert unexpected_amount.error.code == "unexpected_amount"
    assert amount_too_large.error.code == "amount_too_large"
    assert before == after


def test_start_next_hand_without_auto_resolve_preserves_normal_turn_when_no_auto_step_is_pending() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Qd", "Ad", "Ks", "Qc", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(2_000, 2_000, 2_000),
    )

    result = engine.start_next_hand(auto_resolve=False)

    assert result.ok is True
    assert engine.has_pending_automatic_progress() is False
    assert engine.get_acting_seat() == "p1"
    assert {item.action_type for item in engine.get_legal_actions("p1")} == {
        ActionType.FOLD,
        ActionType.CALL,
        ActionType.RAISE,
    }


def test_manual_automatic_progress_hides_turn_until_street_transition_is_resolved() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(2_000, 2_000),
    )
    engine.start_next_hand(auto_resolve=False)

    assert engine.apply_action("p1", PlayerAction(ActionType.CALL), auto_resolve=False).ok is True
    result = engine.apply_action("p2", PlayerAction(ActionType.CHECK), auto_resolve=False)

    assert result.ok is True
    assert engine.has_pending_automatic_progress() is True
    assert engine.get_acting_seat() is None
    assert engine.get_legal_actions("p2") == ()

    blocked = engine.apply_action("p2", PlayerAction(ActionType.CHECK), auto_resolve=False)
    assert blocked.ok is False
    assert blocked.error is not None
    assert blocked.error.code == "automatic_progress_pending"

    progress = engine.resolve_automatic_step()

    assert progress.advanced is True
    assert [event.event_type for event in progress.events] == ["street_started"]
    assert engine.get_phase() == GamePhase.FLOP
    assert engine.has_pending_automatic_progress() is False
    assert engine.get_acting_seat() == "p2"


def test_manual_automatic_progress_steps_all_in_runout_one_transition_at_a_time() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(500, 500),
    )
    engine.start_next_hand(auto_resolve=False)

    assert engine.apply_action("p1", PlayerAction(ActionType.RAISE, amount=500), auto_resolve=False).ok is True
    assert engine.apply_action("p2", PlayerAction(ActionType.CALL), auto_resolve=False).ok is True

    assert engine.has_pending_automatic_progress() is True

    first = engine.resolve_automatic_step()
    second = engine.resolve_automatic_step()
    third = engine.resolve_automatic_step()
    fourth = engine.resolve_automatic_step()

    assert first.advanced is True
    assert [event.payload.get("phase") for event in first.events if event.event_type == "street_started"] == ["flop"]
    assert second.advanced is True
    assert [event.payload.get("phase") for event in second.events if event.event_type == "street_started"] == ["turn"]
    assert third.advanced is True
    assert [event.payload.get("phase") for event in third.events if event.event_type == "street_started"] == ["river"]
    assert fourth.advanced is True
    assert any(event.event_type == "showdown_started" for event in fourth.events)
    assert engine.is_hand_complete() is True
    assert engine.resolve_automatic_step().advanced is False


# ---------------------------------------------------------------------------
# Edge case: hand evaluation
# ---------------------------------------------------------------------------


def test_flush_beats_straight() -> None:
    # Flush (5,*) vs straight (4,*)
    flush = rank_five_cards(("Ah", "9h", "5h", "3h", "2h"))
    straight = rank_five_cards(("9s", "8h", "7d", "6c", "5s"))
    assert flush > straight


def test_two_pair_kicker_decides_winner() -> None:
    hand_a = best_hand_rank(("Ks", "Qd", "Kh", "Qc", "Ah", "2d", "3c"))
    hand_b = best_hand_rank(("Ks", "Qd", "Kh", "Qc", "7h", "2d", "3c"))
    assert hand_a > hand_b


def test_full_house_trips_rank_takes_precedence() -> None:
    higher = rank_five_cards(("Ah", "As", "Ad", "Kh", "Ks"))
    lower = rank_five_cards(("Kh", "Ks", "Kd", "Ah", "As"))
    assert higher > lower


def test_best_hand_rank_requires_five_cards() -> None:
    with pytest.raises(ValueError, match="five cards"):
        best_hand_rank(("As", "Kh", "Qd", "Jc"))


# ---------------------------------------------------------------------------
# Edge case: all fold to big blind (3 players)
# ---------------------------------------------------------------------------


def test_all_fold_to_big_blind_awards_pot_without_bb_acting() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Qd", "Ad", "Ks", "Qc", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(2_000, 2_000, 2_000),
    )
    engine.start_next_hand()

    # 3 players: p1=dealer, p2=SB, p3=BB. First to act preflop = p1 (UTG).
    assert engine.get_acting_seat() == "p1"
    engine.apply_action("p1", PlayerAction(ActionType.FOLD))
    assert engine.get_acting_seat() == "p2"
    result = engine.apply_action("p2", PlayerAction(ActionType.FOLD))

    assert result.ok is True
    assert engine.is_hand_complete() is True
    stacks = {s.seat_id: s.stack for s in engine.get_public_table_view().seats}
    assert stacks == {"p1": 2_000, "p2": 1_950, "p3": 2_050}


# ---------------------------------------------------------------------------
# Edge case: BB option after all limps (3 players)
# ---------------------------------------------------------------------------


def test_big_blind_gets_option_after_all_limps() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Qd", "Ad", "Ks", "Qc", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(2_000, 2_000, 2_000),
    )
    engine.start_next_hand()

    # UTG (p1) calls, SB (p2) calls → BB (p3) should still get to act
    engine.apply_action("p1", PlayerAction(ActionType.CALL))
    engine.apply_action("p2", PlayerAction(ActionType.CALL))

    assert engine.get_acting_seat() == "p3"
    legal = {a.action_type for a in engine.get_legal_actions("p3")}
    assert ActionType.CHECK in legal
    # BB can check (to_call == 0)
    result = engine.apply_action("p3", PlayerAction(ActionType.CHECK))
    assert result.ok is True
    assert engine.get_phase() == GamePhase.FLOP


# ---------------------------------------------------------------------------
# Edge case: partial blind (SB cannot afford full small blind)
# ---------------------------------------------------------------------------


def test_partial_small_blind_posts_remaining_stack() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Qd", "Ad", "Ks", "Qc", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(2_000, 30, 2_000),
    )
    result = engine.start_next_hand()

    assert result.ok is True
    blind_events = [e for e in result.events if e.event_type == "blind_posted"]
    sb_event = next(e for e in blind_events if e.payload["blind"] == "small")
    assert sb_event.payload["amount"] == 30  # only 30 of the 50 SB


# ---------------------------------------------------------------------------
# Edge case: chip conservation across multiple hands
# ---------------------------------------------------------------------------


def test_chip_conservation_across_hands() -> None:
    """Total chips in play must remain constant across hands."""
    deck1 = ("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc")
    deck2 = ("Qs", "Jh", "Qd", "Jd", "2h", "3c", "4d", "5s", "6c")
    engine = PokerEngine.create_table(
        TableConfig(deck_factory=DeckSequenceFactory([deck1, deck2])),
        [SeatConfig("p1", "P1"), SeatConfig("p2", "P2")],
    )
    initial_total = sum(s.stack for s in engine.get_public_table_view().seats)

    # Hand 1: p1 folds
    engine.start_next_hand()
    engine.apply_action("p1", PlayerAction(ActionType.FOLD))
    total_after_h1 = sum(s.stack for s in engine.get_public_table_view().seats)
    assert total_after_h1 == initial_total

    # Hand 2: p2 folds
    engine.start_next_hand()
    engine.apply_action("p2", PlayerAction(ActionType.FOLD))
    total_after_h2 = sum(s.stack for s in engine.get_public_table_view().seats)
    assert total_after_h2 == initial_total


def test_chip_conservation_across_hands_with_ante() -> None:
    deck1 = ("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc")
    deck2 = ("Qs", "Jh", "Qd", "Jd", "2h", "3c", "4d", "5s", "6c")
    engine = PokerEngine.create_table(
        TableConfig(ante=10, deck_factory=DeckSequenceFactory([deck1, deck2])),
        [SeatConfig("p1", "P1"), SeatConfig("p2", "P2")],
    )
    initial_total = sum(s.stack for s in engine.get_public_table_view().seats)

    engine.start_next_hand()
    engine.apply_action("p1", PlayerAction(ActionType.FOLD))
    total_after_h1 = sum(s.stack for s in engine.get_public_table_view().seats)
    assert total_after_h1 == initial_total

    engine.start_next_hand()
    engine.apply_action("p2", PlayerAction(ActionType.FOLD))
    total_after_h2 = sum(s.stack for s in engine.get_public_table_view().seats)
    assert total_after_h2 == initial_total


# ---------------------------------------------------------------------------
# Edge case: heads-up both all-in preflop triggers auto-runout
# ---------------------------------------------------------------------------


def test_heads_up_both_all_in_auto_runout() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(500, 500),
    )
    engine.start_next_hand()

    # Dealer/SB raises all-in
    engine.apply_action("p1", PlayerAction(ActionType.RAISE, amount=500))
    result = engine.apply_action("p2", PlayerAction(ActionType.CALL))

    assert result.ok is True
    assert engine.is_hand_complete() is True
    stacks = {s.seat_id: s.stack for s in engine.get_public_table_view().seats}
    assert stacks["p1"] + stacks["p2"] == 1_000  # chip conservation
    event_types = [e.event_type for e in result.events]
    assert "showdown_started" in event_types


# ---------------------------------------------------------------------------
# Edge case: action on wrong phase rejected
# ---------------------------------------------------------------------------


def test_action_rejected_when_hand_not_in_progress() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Ad", "Kd", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(2_000, 2_000),
    )
    # Before starting any hand
    result = engine.apply_action("p1", PlayerAction(ActionType.FOLD))
    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "wrong_phase"


# ---------------------------------------------------------------------------
# Edge case: deck exhaustion during deal refunds blinds
# ---------------------------------------------------------------------------


def test_deck_exhaustion_during_deal_refunds_blinds() -> None:
    # Only 3 cards — enough for blinds but not for dealing hole cards
    engine = PokerEngine.create_table(
        TableConfig(ante=5, deck_factory=DeckSequenceFactory([("As", "Kh", "Qd")])),
        [SeatConfig("p1", "P1"), SeatConfig("p2", "P2")],
    )
    initial_total = sum(s.stack for s in engine.get_public_table_view().seats)

    result = engine.start_next_hand()

    assert result.ok is False
    assert result.error is not None
    assert result.error.code == "deck_exhausted"
    assert [event.event_type for event in result.events[:5]] == [
        "hand_started",
        "ante_posted",
        "ante_posted",
        "blind_posted",
        "blind_posted",
    ]
    refunds = [event.payload for event in result.events if event.event_type == "chips_refunded"]
    assert refunds == [
        {"seat_id": "p1", "amount": 55},
        {"seat_id": "p2", "amount": 105},
    ]
    final_total = sum(s.stack for s in engine.get_public_table_view().seats)
    assert final_total == initial_total  # all chips refunded


# ---------------------------------------------------------------------------
# Edge case: bet then raise then re-raise in multiway
# ---------------------------------------------------------------------------


def test_bet_raise_reraise_sequence() -> None:
    engine = make_engine(
        deck=("As", "Kh", "Qd", "Ad", "Ks", "Qc", "2c", "7d", "8h", "9s", "Tc"),
        stacks=(2_000, 2_000, 2_000),
    )
    engine.start_next_hand()

    # Preflop: all call to see flop
    engine.apply_action("p1", PlayerAction(ActionType.CALL))
    engine.apply_action("p2", PlayerAction(ActionType.CALL))
    engine.apply_action("p3", PlayerAction(ActionType.CHECK))

    # Flop: p2 bets, p3 raises, p1 re-raises
    assert engine.get_phase() == GamePhase.FLOP
    # Post-flop action starts left of dealer (p2)
    engine.apply_action("p2", PlayerAction(ActionType.BET, amount=200))
    engine.apply_action("p3", PlayerAction(ActionType.RAISE, amount=400))

    # p1 should be able to re-raise (full raise was made)
    legal = {a.action_type for a in engine.get_legal_actions("p1")}
    assert ActionType.RAISE in legal
    result = engine.apply_action("p1", PlayerAction(ActionType.RAISE, amount=600))
    assert result.ok is True
