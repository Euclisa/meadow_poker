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
VALUE_NAMES_SINGULAR = {
    2: "two",
    3: "three",
    4: "four",
    5: "five",
    6: "six",
    7: "seven",
    8: "eight",
    9: "nine",
    10: "ten",
    11: "jack",
    12: "queen",
    13: "king",
    14: "ace",
}
VALUE_NAMES_PLURAL = {
    2: "twos",
    3: "threes",
    4: "fours",
    5: "fives",
    6: "sixes",
    7: "sevens",
    8: "eights",
    9: "nines",
    10: "tens",
    11: "jacks",
    12: "queens",
    13: "kings",
    14: "aces",
}


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


def best_hand_details(cards: tuple[str, ...]) -> tuple[tuple[int, ...], str]:
    if len(cards) < 5:
        raise ValueError("At least five cards are required to evaluate a hand")
    best_combo = max(combinations(cards, 5), key=rank_five_cards)
    rank = rank_five_cards(best_combo)
    return rank, _label_rank(rank)


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


def _label_rank(rank: tuple[int, ...]) -> str:
    category = rank[0]
    if category == 8:
        return f"straight flush, {_high_label(rank[1])}"
    if category == 7:
        return f"four of a kind, {_plural_name(rank[1])}"
    if category == 6:
        return f"full house, {_plural_name(rank[1])} full of {_plural_name(rank[2])}"
    if category == 5:
        return f"flush, {_high_label(rank[1])}"
    if category == 4:
        return f"straight, {_high_label(rank[1])}"
    if category == 3:
        return f"three of a kind, {_plural_name(rank[1])}"
    if category == 2:
        return f"two pair, {_plural_name(rank[1])} and {_plural_name(rank[2])}"
    if category == 1:
        return f"one pair, {_plural_name(rank[1])}"
    return f"high card, {_high_label(rank[1])}"


def _high_label(value: int) -> str:
    return f"{_singular_name(value)}-high"


def _singular_name(value: int) -> str:
    return VALUE_NAMES_SINGULAR[value]


def _plural_name(value: int) -> str:
    return VALUE_NAMES_PLURAL[value]
