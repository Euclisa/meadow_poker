from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING
from typing import Any

if TYPE_CHECKING:
    from poker_bot.poker.decks import DeckFactory


class ActionType(str, Enum):
    FOLD = "fold"
    CHECK = "check"
    CALL = "call"
    BET = "bet"
    RAISE = "raise"


class GamePhase(str, Enum):
    WAITING_FOR_PLAYERS = "waiting_for_players"
    READY_FOR_HAND = "ready_for_hand"
    PREFLOP = "preflop"
    FLOP = "flop"
    TURN = "turn"
    RIVER = "river"
    SHOWDOWN = "showdown"
    HAND_COMPLETE = "hand_complete"
    TABLE_COMPLETE = "table_complete"


class TelegramTableState(str, Enum):
    WAITING = "waiting"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class TableConfig:
    small_blind: int = 50
    big_blind: int = 100
    starting_stack: int = 2_000
    min_players: int = 2
    max_players: int = 6
    deck_factory: DeckFactory | None = None

    def __post_init__(self) -> None:
        if self.small_blind <= 0 or self.big_blind <= 0:
            raise ValueError("Blinds must be positive")
        if self.small_blind > self.big_blind:
            raise ValueError("Small blind cannot exceed big blind")
        if self.min_players < 2:
            raise ValueError("At least two players are required")
        if self.max_players < self.min_players:
            raise ValueError("max_players must be >= min_players")
        if self.starting_stack <= 0:
            raise ValueError("starting_stack must be positive")


@dataclass(frozen=True, slots=True)
class SeatConfig:
    seat_id: str
    name: str
    starting_stack: int | None = None


@dataclass(frozen=True, slots=True)
class PlayerAction:
    action_type: ActionType
    amount: int | None = None


@dataclass(frozen=True, slots=True)
class ActionValidationError:
    code: str
    message: str


@dataclass(frozen=True, slots=True)
class LegalAction:
    action_type: ActionType
    min_amount: int | None = None
    max_amount: int | None = None


@dataclass(frozen=True, slots=True)
class GameEvent:
    event_type: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class SeatSnapshot:
    seat_id: str
    name: str
    stack: int
    contribution: int
    folded: bool
    all_in: bool
    in_hand: bool
    position: str | None


@dataclass(frozen=True, slots=True)
class PublicTableView:
    hand_number: int
    phase: GamePhase
    board_cards: tuple[str, ...]
    pot_total: int
    current_bet: int
    dealer_seat_id: str | None
    acting_seat_id: str | None
    small_blind: int
    big_blind: int
    seats: tuple[SeatSnapshot, ...]


@dataclass(frozen=True, slots=True)
class PlayerView:
    seat_id: str
    player_name: str
    hole_cards: tuple[str, ...]
    stack: int
    contribution: int
    position: str | None
    to_call: int
    public_table: PublicTableView


@dataclass(frozen=True, slots=True)
class DecisionRequest:
    acting_seat_id: str
    player_view: PlayerView
    public_table_view: PublicTableView
    legal_actions: tuple[LegalAction, ...]
    recent_events: tuple[GameEvent, ...]
    validation_error: ActionValidationError | None = None


@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    error: ActionValidationError | None = None
    events: tuple[GameEvent, ...] = ()
    state_changed: bool = False


@dataclass(frozen=True, slots=True)
class TelegramTableCreateRequest:
    total_seats: int
    llm_seat_count: int

    @property
    def telegram_seat_count(self) -> int:
        return self.total_seats - self.llm_seat_count


@dataclass(frozen=True, slots=True)
class TelegramTableStatus:
    table_id: str
    status: TelegramTableState
    creator_user_id: int
    total_seats: int
    telegram_seats_total: int
    telegram_seats_claimed: int
    llm_seat_count: int
    joined_user_ids: tuple[int, ...]


@dataclass(slots=True)
class TelegramPendingActionState:
    seat_id: str
    user_id: int
    chat_id: int
    decision_request: DecisionRequest
    selected_action_type: ActionType | None = None
    awaiting_amount: bool = False
