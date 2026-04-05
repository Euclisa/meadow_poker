from __future__ import annotations

from poker_bot.hand_history import render_live_public_hand_summary, render_public_completed_hand_summary
from poker_bot.types import GameEvent, GamePhase, HandRecord, HandRecordStatus, PublicTableView, SeatSnapshot


def _make_public_view(*, hand_number: int, phase: GamePhase, board_cards: tuple[str, ...]) -> PublicTableView:
    return PublicTableView(
        hand_number=hand_number,
        phase=phase,
        board_cards=board_cards,
        pot_total=200,
        current_bet=100,
        dealer_seat_id="p1",
        acting_seat_id="p1" if phase != GamePhase.HAND_COMPLETE else None,
        small_blind=50,
        big_blind=100,
        seats=(
            SeatSnapshot("p1", "Hero", 1900, 100, False, False, True, "dealer"),
            SeatSnapshot("p2", "Villain", 2100, 100, False, False, True, "big_blind"),
        ),
    )


def test_public_live_hand_summary_uses_only_public_information() -> None:
    record = HandRecord(
        hand_number=3,
        status=HandRecordStatus.IN_PROGRESS,
        events=(
            GameEvent("hand_started", {"hand_number": 3}),
            GameEvent("blind_posted", {"seat_id": "p1", "blind": "small", "amount": 50}),
            GameEvent("blind_posted", {"seat_id": "p2", "blind": "big", "amount": 100}),
            GameEvent("street_started", {"phase": "preflop", "board_cards": ()}),
            GameEvent("action_applied", {"seat_id": "p1", "action": "call", "amount": 100}),
            GameEvent("action_applied", {"seat_id": "p2", "action": "check"}),
            GameEvent("street_started", {"phase": "flop", "board_cards": ("2c", "7d", "8h")}),
            GameEvent("action_applied", {"seat_id": "p2", "action": "bet", "amount": 100}),
        ),
        start_public_view=_make_public_view(hand_number=3, phase=GamePhase.PREFLOP, board_cards=()),
        current_public_view=_make_public_view(
            hand_number=3,
            phase=GamePhase.FLOP,
            board_cards=("2c", "7d", "8h"),
        ),
        ended_in_showdown=False,
    )

    summary = render_live_public_hand_summary(record)

    assert "Hand #3" in summary
    assert "Villain bet 100" in summary
    assert "Current stacks:" in summary
    assert "As Kd" not in summary


def test_public_completed_hand_summary_stays_public_without_showdown_cards() -> None:
    record = HandRecord(
        hand_number=4,
        status=HandRecordStatus.COMPLETED,
        events=(
            GameEvent("hand_started", {"hand_number": 4}),
            GameEvent("blind_posted", {"seat_id": "p1", "blind": "small", "amount": 50}),
            GameEvent("blind_posted", {"seat_id": "p2", "blind": "big", "amount": 100}),
            GameEvent("street_started", {"phase": "preflop", "board_cards": ()}),
            GameEvent("action_applied", {"seat_id": "p1", "action": "fold"}),
            GameEvent("hand_awarded", {"seat_id": "p2", "amount": 150}),
            GameEvent("hand_completed", {"hand_number": 4}),
        ),
        start_public_view=_make_public_view(hand_number=4, phase=GamePhase.PREFLOP, board_cards=()),
        current_public_view=_make_public_view(
            hand_number=4,
            phase=GamePhase.HAND_COMPLETE,
            board_cards=(),
        ),
        ended_in_showdown=False,
    )

    summary = render_public_completed_hand_summary(record)

    assert "Hand #4" in summary
    assert "Result:" in summary
    assert "Villain collected 150" in summary
    assert "Your hole cards" not in summary
    assert "As Kd" not in summary
