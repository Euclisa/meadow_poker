from __future__ import annotations

from itertools import combinations
import random

RANK_TO_VALUE = {
    "2": 2,
    "3": 3,
    "4": 4,
    "5": 5,
    "6": 6,
    "7": 7,
    "8": 8,
    "9": 9,
    "T": 10,
    "J": 11,
    "Q": 12,
    "K": 13,
    "A": 14,
}
VALUE_TO_RANK = {value: rank for rank, value in RANK_TO_VALUE.items()}
SUITS = ("c", "d", "h", "s")


def validate_card(card: str) -> str:
    if len(card) != 2:
        raise ValueError(f"Invalid card: {card}")
    rank, suit = card[0], card[1].lower()
    if rank not in RANK_TO_VALUE or suit not in SUITS:
        raise ValueError(f"Invalid card: {card}")
    return f"{rank}{suit}"


def make_deck(preloaded_cards: tuple[str, ...] | None = None) -> list[str]:
    if preloaded_cards is not None:
        return [validate_card(card) for card in preloaded_cards]
    deck = [f"{rank}{suit}" for rank in RANK_TO_VALUE for suit in SUITS]
    random.shuffle(deck)
    return deck


def rank_five_cards(cards: tuple[str, ...]) -> tuple[int, ...]:
    values = sorted((RANK_TO_VALUE[card[0]] for card in cards), reverse=True)
    suits = [card[1] for card in cards]
    counts: dict[int, int] = {}
    for value in values:
        counts[value] = counts.get(value, 0) + 1

    ordered_counts = sorted(
        counts.items(),
        key=lambda item: (item[1], item[0]),
        reverse=True,
    )

    is_flush = len(set(suits)) == 1
    unique_values = sorted(set(values), reverse=True)
    straight_high = _straight_high(unique_values)

    if is_flush and straight_high is not None:
        return (8, straight_high)
    if ordered_counts[0][1] == 4:
        four = ordered_counts[0][0]
        kicker = max(value for value in values if value != four)
        return (7, four, kicker)
    if ordered_counts[0][1] == 3 and ordered_counts[1][1] == 2:
        return (6, ordered_counts[0][0], ordered_counts[1][0])
    if is_flush:
        return (5, *values)
    if straight_high is not None:
        return (4, straight_high)
    if ordered_counts[0][1] == 3:
        trips = ordered_counts[0][0]
        kickers = sorted((value for value in values if value != trips), reverse=True)
        return (3, trips, *kickers)
    if ordered_counts[0][1] == 2 and ordered_counts[1][1] == 2:
        high_pair = max(ordered_counts[0][0], ordered_counts[1][0])
        low_pair = min(ordered_counts[0][0], ordered_counts[1][0])
        kicker = max(value for value in values if value not in (high_pair, low_pair))
        return (2, high_pair, low_pair, kicker)
    if ordered_counts[0][1] == 2:
        pair = ordered_counts[0][0]
        kickers = sorted((value for value in values if value != pair), reverse=True)
        return (1, pair, *kickers)
    return (0, *values)


def best_hand_rank(cards: tuple[str, ...]) -> tuple[int, ...]:
    if len(cards) < 5:
        raise ValueError("At least five cards are required to evaluate a hand")
    return max(rank_five_cards(combo) for combo in combinations(cards, 5))


def _straight_high(unique_values: list[int]) -> int | None:
    if len(unique_values) < 5:
        return None

    working = list(unique_values)
    if 14 in working:
        working.append(1)

    for index in range(len(working) - 4):
        window = working[index : index + 5]
        if window[0] - window[4] == 4 and len(set(window)) == 5:
            return 5 if window == [5, 4, 3, 2, 1] else window[0]
    return None
