from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from poker_bot.coach import TableCoach
from poker_bot.orchestrator import GameOrchestrator
from poker_bot.players.base import PlayerAgent
from poker_bot.types import HandArchive, HandRecord, TelegramTableState


@dataclass(frozen=True, slots=True)
class WebTableCreateRequest:
    total_seats: int
    llm_seat_count: int
    big_blind: int
    stack_depth: int

    @property
    def web_seat_count(self) -> int:
        return self.total_seats - self.llm_seat_count

    @property
    def small_blind(self) -> int:
        return self.big_blind // 2

    @property
    def starting_stack(self) -> int:
        return self.big_blind * self.stack_depth


@dataclass(frozen=True, slots=True)
class WebUserReservation:
    seat_id: str
    seat_token: str
    display_name: str


@dataclass(frozen=True, slots=True)
class WebShowdownReveal:
    seat_id: str
    hole_cards: tuple[str, str]


@dataclass(frozen=True, slots=True)
class WebShowdownWinner:
    seat_id: str
    amount: int


@dataclass(frozen=True, slots=True)
class WebShowdownState:
    revealed_seats: tuple[WebShowdownReveal, ...]
    winners: tuple[WebShowdownWinner, ...]


@dataclass(slots=True)
class WebTableSession:
    table_id: str
    creator_seat_token: str
    request: WebTableCreateRequest
    claimed_web_users: list[WebUserReservation]
    status: TelegramTableState = TelegramTableState.WAITING
    engine: object | None = None
    orchestrator: GameOrchestrator | None = None
    coach: TableCoach | None = None
    player_agents: dict[str, PlayerAgent] = field(default_factory=dict)
    orchestrator_task: asyncio.Task[Any] | None = None
    status_message: str = "Waiting for players."
    activity_log: list[dict[str, Any]] = field(default_factory=list)
    showdown_state: WebShowdownState | None = None
    _activity_counter: int = 0
    _watchers: set[asyncio.Queue[str]] = field(default_factory=set)

    @property
    def total_seats(self) -> int:
        return self.request.total_seats

    @property
    def llm_seat_count(self) -> int:
        return self.request.llm_seat_count

    @property
    def web_seat_count(self) -> int:
        return self.request.web_seat_count

    @property
    def human_player_count(self) -> int:
        return len(self.claimed_web_users)

    def is_full(self) -> bool:
        return self.human_player_count >= self.web_seat_count

    def has_multiple_human_players(self) -> bool:
        return self.web_seat_count > 1

    def open_web_seat_count(self) -> int:
        return max(0, self.web_seat_count - self.human_player_count)

    def is_creator_token(self, seat_token: str | None) -> bool:
        return seat_token is not None and seat_token == self.creator_seat_token

    def human_users(self) -> tuple[WebUserReservation, ...]:
        return tuple(self.claimed_web_users)

    def add_activity(self, *, kind: str, text: str) -> None:
        self._activity_counter += 1
        self.activity_log.append(
            {
                "id": f"wait-{self._activity_counter}",
                "kind": kind,
                "text": text,
            }
        )

    def claim_web_user(self, reservation: WebUserReservation) -> None:
        if self.status != TelegramTableState.WAITING:
            raise ValueError("Only waiting tables can accept new web users")
        if self.is_full():
            raise ValueError("No open web seats remain")
        if self.find_reservation_by_token(reservation.seat_token) is not None:
            raise ValueError("Seat token is already assigned at this table")
        if self.find_reservation_by_name(reservation.display_name) is not None:
            raise ValueError("Display name is already taken at this table")
        self.claimed_web_users.append(reservation)

    def remove_web_user(self, seat_token: str) -> WebUserReservation | None:
        for index, user in enumerate(self.claimed_web_users):
            if user.seat_token == seat_token:
                return self.claimed_web_users.pop(index)
        return None

    def find_reservation_by_token(self, seat_token: str | None) -> WebUserReservation | None:
        if seat_token is None:
            return None
        return next((user for user in self.claimed_web_users if user.seat_token == seat_token), None)

    def find_reservation_by_name(self, display_name: str) -> WebUserReservation | None:
        lookup = display_name.strip().casefold()
        return next((user for user in self.claimed_web_users if user.display_name.casefold() == lookup), None)

    def subscribe(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._watchers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[str]) -> None:
        self._watchers.discard(queue)

    def notify_watchers(self) -> None:
        for queue in list(self._watchers):
            if queue.qsize() > 1:
                continue
            try:
                queue.put_nowait("update")
            except RuntimeError:
                self._watchers.discard(queue)

    def find_completed_hand(self, hand_number: int) -> HandRecord | None:
        archive = self.find_completed_hand_archive(hand_number)
        return archive.record if archive is not None else None

    def find_completed_hand_archive(self, hand_number: int) -> HandArchive | None:
        if self.orchestrator is None:
            return None
        return next(
            (archive for archive in self.orchestrator.completed_hand_archives if archive.record.hand_number == hand_number),
            None,
        )
