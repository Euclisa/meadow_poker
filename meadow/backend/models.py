from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

from meadow.types import HandArchive, HandRecord, TelegramTableState


@dataclass(frozen=True, slots=True)
class ActorRef:
    transport: str
    external_id: str
    display_name: str
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def actor_key(self) -> tuple[str, str]:
        return (self.transport, self.external_id)


@dataclass(frozen=True, slots=True)
class ManagedTableConfig:
    total_seats: int
    llm_seat_count: int
    small_blind: int
    big_blind: int
    ante: int = 0
    starting_stack: int = 2_000
    turn_timeout_seconds: int | None = None
    max_hands_per_table: int | None = None
    max_players: int = 6
    human_transport: str = "web"
    human_seat_prefix: str = "web"
    stack_depth: int | None = None

    @property
    def human_seat_count(self) -> int:
        return self.total_seats - self.llm_seat_count


@dataclass(frozen=True, slots=True)
class SeatReservation:
    seat_id: str | None
    viewer_token: str
    actor: ActorRef

    @property
    def is_seated(self) -> bool:
        return self.seat_id is not None


@dataclass(frozen=True, slots=True)
class ShowdownReveal:
    seat_id: str
    hole_cards: tuple[str, str]


@dataclass(frozen=True, slots=True)
class ShowdownWinner:
    seat_id: str
    amount: int


@dataclass(frozen=True, slots=True)
class ShowdownState:
    revealed_seats: tuple[ShowdownReveal, ...]
    winners: tuple[ShowdownWinner, ...]


@dataclass(slots=True)
class BackendTableRuntime:
    table_id: str
    config: ManagedTableConfig
    creator_viewer_token: str
    reservations: list[SeatReservation]
    status: TelegramTableState = TelegramTableState.WAITING
    engine: object | None = None
    orchestrator: object | None = None
    coach: object | None = None
    player_agents: dict[str, Any] = field(default_factory=dict)
    human_agents: dict[str, Any] = field(default_factory=dict)
    orchestrator_task: Any | None = None
    status_message: str = "Waiting for players."
    activity_log: list[dict[str, Any]] = field(default_factory=list)
    showdown_state: ShowdownState | None = None
    version: int = 1
    _activity_counter: int = 0
    _published_event_index: int = 0
    _versioned_events: list[tuple[int, tuple[Any, ...]]] = field(default_factory=list)

    @property
    def total_seats(self) -> int:
        return self.config.total_seats

    @property
    def llm_seat_count(self) -> int:
        return self.config.llm_seat_count

    @property
    def human_seat_count(self) -> int:
        return self.config.human_seat_count

    @property
    def human_transport(self) -> str:
        return self.config.human_transport

    @property
    def human_player_count(self) -> int:
        return sum(1 for reservation in self.reservations if reservation.is_seated)

    @property
    def request(self) -> ManagedTableConfig:
        return self.config

    @property
    def telegram_seat_count(self) -> int:
        return self.human_seat_count if self.human_transport == "telegram" else 0

    @property
    def web_seat_count(self) -> int:
        return self.human_seat_count if self.human_transport == "web" else 0

    @property
    def claimed_telegram_users(self) -> list[Any]:
        if self.human_transport != "telegram":
            return []
        return [
            SimpleNamespace(
                user_id=int(reservation.actor.external_id),
                chat_id=int(reservation.actor.metadata.get("chat_id", 0)),
                display_name=reservation.actor.display_name,
            )
            for reservation in self.reservations
            if reservation.is_seated
        ]

    @property
    def claimed_web_users(self) -> list[Any]:
        if self.human_transport != "web":
            return []
        return [
            SimpleNamespace(
                seat_id=reservation.seat_id,
                seat_token=reservation.viewer_token,
                display_name=reservation.actor.display_name,
            )
            for reservation in self.reservations
            if reservation.is_seated and reservation.seat_id is not None
        ]

    def is_full(self) -> bool:
        return self.human_player_count >= self.human_seat_count

    def is_creator_token(self, viewer_token: str | None) -> bool:
        return viewer_token is not None and viewer_token == self.creator_viewer_token

    def open_human_seat_count(self) -> int:
        return max(0, self.human_seat_count - self.human_player_count)

    def find_reservation_by_token(self, viewer_token: str | None) -> SeatReservation | None:
        if viewer_token is None:
            return None
        return next((item for item in self.reservations if item.viewer_token == viewer_token), None)

    def find_seated_reservation_by_token(self, viewer_token: str | None) -> SeatReservation | None:
        reservation = self.find_reservation_by_token(viewer_token)
        if reservation is None or not reservation.is_seated:
            return None
        return reservation

    def find_reservation_by_actor(self, actor: ActorRef) -> SeatReservation | None:
        return next((item for item in self.reservations if item.actor.actor_key == actor.actor_key), None)

    def find_seated_reservation_by_actor(self, actor: ActorRef) -> SeatReservation | None:
        return next(
            (
                item
                for item in self.reservations
                if item.is_seated and item.actor.actor_key == actor.actor_key
            ),
            None,
        )

    def find_reservation_by_name(self, display_name: str) -> SeatReservation | None:
        lookup = display_name.strip().casefold()
        return next((item for item in self.reservations if item.actor.display_name.casefold() == lookup), None)

    def find_seated_reservation_by_name(self, display_name: str) -> SeatReservation | None:
        lookup = display_name.strip().casefold()
        return next(
            (
                item
                for item in self.reservations
                if item.is_seated and item.actor.display_name.casefold() == lookup
            ),
            None,
        )

    def seated_reservations(self) -> list[SeatReservation]:
        return [reservation for reservation in self.reservations if reservation.is_seated]

    def add_activity(self, *, kind: str, text: str) -> None:
        self._activity_counter += 1
        self.activity_log.append(
            {
                "id": f"activity-{self._activity_counter}",
                "kind": kind,
                "text": text,
            }
        )

    def find_completed_hand(self, hand_number: int) -> HandRecord | None:
        archive = self.find_completed_hand_archive(hand_number)
        return archive.record if archive is not None else None

    def find_completed_hand_archive(self, hand_number: int) -> HandArchive | None:
        orchestrator = self.orchestrator
        if orchestrator is None:
            return None
        return next(
            (
                archive
                for archive in orchestrator.completed_hand_archives
                if archive.record.hand_number == hand_number
            ),
            None,
        )
