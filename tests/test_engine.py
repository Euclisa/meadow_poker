from poker_bot.poker.cards import best_hand_rank
from poker_bot.poker.decks import PredefinedDeckFactory
from poker_bot.poker.engine import PokerEngine
from poker_bot.types import ActionType, PlayerAction, SeatConfig, TableConfig


def make_engine(
    *,
    deck: tuple[str, ...],
    stacks: tuple[int, ...],
    small_blind: int = 50,
    big_blind: int = 100,
) -> PokerEngine:
    seats = [
        SeatConfig(seat_id=f"p{index + 1}", name=f"P{index + 1}", starting_stack=stack)
        for index, stack in enumerate(stacks)
    ]
    return PokerEngine.create_table(
        TableConfig(
            small_blind=small_blind,
            big_blind=big_blind,
            deck_factory=PredefinedDeckFactory([deck]),
        ),
        seats,
    )


def test_best_hand_rank_handles_wheel_straight() -> None:
    assert best_hand_rank(("As", "2d", "3c", "4h", "5s")) == (4, 5)


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


def test_scripted_deck_factory_ends_table_cleanly_when_out_of_hands() -> None:
    engine = PokerEngine.create_table(
        TableConfig(
            deck_factory=PredefinedDeckFactory(
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
