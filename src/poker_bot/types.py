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


class HandRecordStatus(str, Enum):
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"


class PlayerUpdateType(str, Enum):
    STATE_CHANGED = "state_changed"
    TURN_STARTED = "turn_started"
    HAND_COMPLETED = "hand_completed"
    TABLE_COMPLETED = "table_completed"


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
    street_contribution: int = 0


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
    validation_error: ActionValidationError | None = None


@dataclass(frozen=True, slots=True)
class PlayerUpdate:
    update_type: PlayerUpdateType
    events: tuple[GameEvent, ...]
    public_table_view: PublicTableView
    player_view: PlayerView
    acting_seat_id: str | None
    is_your_turn: bool


@dataclass(frozen=True, slots=True)
class HandRecord:
    hand_number: int
    status: HandRecordStatus
    events: tuple[GameEvent, ...]
    start_public_view: PublicTableView
    current_public_view: PublicTableView
    ended_in_showdown: bool


@dataclass(frozen=True, slots=True)
class HandRunResult:
    started: bool
    hand_number: int | None
    ended_in_showdown: bool
    table_complete: bool
    events: tuple[GameEvent, ...] = ()
    completed_hand: HandRecord | None = None


@dataclass(frozen=True, slots=True)
class ActionResult:
    ok: bool
    error: ActionValidationError | None = None
    events: tuple[GameEvent, ...] = ()
    state_changed: bool = False


@dataclass(frozen=True, slots=True)
class AutomaticProgressResult:
    advanced: bool
    events: tuple[GameEvent, ...] = ()


@dataclass(frozen=True, slots=True)
class HandSeatState:
    seat_id: str
    name: str
    stack: int
    hole_cards: tuple[str, ...]
    folded: bool
    all_in: bool
    in_hand: bool
    committed_this_street: int
    committed_this_hand: int
    has_acted_this_round: bool
    last_action_bet_level: int
    position: str | None


@dataclass(frozen=True, slots=True)
class HandStateSnapshot:
    hand_number: int
    phase: GamePhase
    board_cards: tuple[str, ...]
    current_bet: int
    last_full_raise_amount: int
    last_full_raise_to: int
    dealer_seat_id: str | None
    acting_seat_id: str | None
    small_blind_seat_id: str | None
    big_blind_seat_id: str | None
    small_blind: int
    big_blind: int
    remaining_deck_order: str
    seats: tuple[HandSeatState, ...]


@dataclass(frozen=True, slots=True)
class HandTransition:
    kind: str
    events: tuple[GameEvent, ...]
    seat_id: str | None = None
    action: PlayerAction | None = None


@dataclass(frozen=True, slots=True)
class HandTrace:
    hand_number: int
    initial_state: HandStateSnapshot
    initial_events: tuple[GameEvent, ...]
    transitions: tuple[HandTransition, ...]
    final_state: HandStateSnapshot | None
    ended_in_showdown: bool

    @property
    def total_steps(self) -> int:
        return len(self.transitions) + 1


@dataclass(frozen=True, slots=True)
class HandArchive:
    record: HandRecord
    trace: HandTrace


@dataclass(frozen=True, slots=True)
class ReplayFrame:
    step_index: int
    total_steps: int
    public_table_view: PublicTableView
    player_view: PlayerView | None
    visible_events: tuple[GameEvent, ...]
    focused_events: tuple[GameEvent, ...]
    revealed_seats: tuple[tuple[str, tuple[str, str]], ...]
    winner_amounts: tuple[tuple[str, int], ...]


@dataclass(frozen=True, slots=True)
class TelegramTableCreateRequest:
    total_seats: int
    llm_seat_count: int
    small_blind: int = 50
    big_blind: int = 100
    starting_stack: int = 2_000

    def __post_init__(self) -> None:
        TableConfig(
            small_blind=self.small_blind,
            big_blind=self.big_blind,
            starting_stack=self.starting_stack,
        )

    @property
    def telegram_seat_count(self) -> int:
        return self.total_seats - self.llm_seat_count


@dataclass(frozen=True, slots=True)
class TelegramTableStatus:
    table_id: str
    status: TelegramTableState
    creator_user_id: int
    total_seats: int
    small_blind: int
    big_blind: int
    starting_stack: int
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
