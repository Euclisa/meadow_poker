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


class DeckFactory(Protocol):
    def create_hand_deck(self, hand_number: int) -> Deck:
        """Create a fresh deck for one hand."""


@dataclass(slots=True)
class RandomDeck:
    _cards: list[str]

    def draw(self) -> str:
        if not self._cards:
            raise DeckExhaustedError("Random deck is empty")
        return self._cards.pop(0)

    def remaining(self) -> int:
        return len(self._cards)


class RandomDeckFactory:
    def create_hand_deck(self, hand_number: int) -> Deck:
        return RandomDeck(make_deck())


@dataclass(slots=True)
class PredefinedDeck:
    _cards: list[str]

    def __init__(self, cards: tuple[str, ...] | list[str]) -> None:
        self._cards = [validate_card(card) for card in cards]

    def draw(self) -> str:
        if not self._cards:
            raise DeckExhaustedError("Predefined deck is empty")
        return self._cards.pop(0)

    def remaining(self) -> int:
        return len(self._cards)


class PredefinedDeckFactory:
    def __init__(self, hand_decks: list[tuple[str, ...]] | tuple[tuple[str, ...], ...]) -> None:
        self._hand_decks = [tuple(cards) for cards in hand_decks]
        self._next_index = 0

    def create_hand_deck(self, hand_number: int) -> Deck:
        if self._next_index >= len(self._hand_decks):
            raise NoMoreDecksError("No predefined hand decks remain")
        cards = self._hand_decks[self._next_index]
        self._next_index += 1
        return PredefinedDeck(cards)
