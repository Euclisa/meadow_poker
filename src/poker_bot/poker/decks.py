from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from poker_bot.poker.cards import make_deck, validate_card


class DeckExhaustedError(RuntimeError):
    """Raised when a deck cannot supply another card."""


class NoMoreDecksError(RuntimeError):
    """Raised when a deck factory cannot provide another hand deck."""


class Deck(Protocol):
    def draw(self) -> str:
        """Draw one card from the top of the deck."""

    def remaining(self) -> int:
        """Return the number of cards left in the deck."""

    def card_order(self) -> tuple[str, ...]:
        """Return the remaining ordered cards in the deck."""


class DeckFactory(Protocol):
    def create_hand_deck(self, hand_number: int) -> Deck:
        """Create a fresh deck for one hand."""


@dataclass(slots=True)
class OrderedDeck:
    _cards: list[str]

    def __init__(self, cards: tuple[str, ...] | list[str]) -> None:
        self._cards = [validate_card(card) for card in cards]

    def draw(self) -> str:
        if not self._cards:
            raise DeckExhaustedError("Ordered deck is empty")
        return self._cards.pop(0)

    def remaining(self) -> int:
        return len(self._cards)

    def card_order(self) -> tuple[str, ...]:
        return tuple(self._cards)


class ShuffledDeckFactory:
    def create_hand_deck(self, hand_number: int) -> Deck:
        del hand_number
        return OrderedDeck(make_deck())


class OrderedDeckFactory:
    def __init__(self, cards: tuple[str, ...] | list[str] | str) -> None:
        self._card_order = decode_card_order(cards) if isinstance(cards, str) else tuple(cards)

    def create_hand_deck(self, hand_number: int) -> Deck:
        del hand_number
        return OrderedDeck(self._card_order)


class DeckSequenceFactory:
    def __init__(
        self,
        hand_decks: list[tuple[str, ...] | list[str] | str] | tuple[tuple[str, ...] | list[str] | str, ...],
    ) -> None:
        self._hand_decks = [
            decode_card_order(cards) if isinstance(cards, str) else tuple(cards)
            for cards in hand_decks
        ]
        self._next_index = 0

    def create_hand_deck(self, hand_number: int) -> Deck:
        del hand_number
        if self._next_index >= len(self._hand_decks):
            raise NoMoreDecksError("No scripted hand decks remain")
        cards = self._hand_decks[self._next_index]
        self._next_index += 1
        return OrderedDeck(cards)


def encode_card_order(cards: tuple[str, ...] | list[str]) -> str:
    return "".join(validate_card(card) for card in cards)


def decode_card_order(raw: str) -> tuple[str, ...]:
    text = raw.strip()
    if not text:
        return ()
    if len(text) % 2 != 0:
        raise ValueError("Encoded card order must contain an even number of characters")
    return tuple(validate_card(text[index : index + 2]) for index in range(0, len(text), 2))
