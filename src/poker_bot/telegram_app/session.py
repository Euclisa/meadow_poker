from __future__ import annotations

from dataclasses import dataclass, field

from poker_bot.orchestrator import GameOrchestrator
from poker_bot.players.base import PlayerAgent
from poker_bot.types import TelegramTableCreateRequest, TelegramTableState, TelegramTableStatus


@dataclass(frozen=True, slots=True)
class TelegramUserReservation:
    user_id: int
    chat_id: int
    display_name: str


@dataclass(slots=True)
class TelegramTableSession:
    table_id: str
    creator_user_id: int
    creator_chat_id: int
    request: TelegramTableCreateRequest
    claimed_telegram_users: list[TelegramUserReservation]
    status: TelegramTableState = TelegramTableState.WAITING
    engine: object | None = None
    orchestrator: GameOrchestrator | None = None
    player_agents: dict[str, PlayerAgent] = field(default_factory=dict)
    orchestrator_task: object | None = None

    @property
    def total_seats(self) -> int:
        return self.request.total_seats

    @property
    def llm_seat_count(self) -> int:
        return self.request.llm_seat_count

    @property
    def telegram_seat_count(self) -> int:
        return self.request.telegram_seat_count

    @property
    def human_player_count(self) -> int:
        return len(self.claimed_telegram_users)

    @property
    def has_multiple_human_players(self) -> bool:
        return self.telegram_seat_count > 1

    def is_full(self) -> bool:
        return self.human_player_count >= self.telegram_seat_count

    def has_user(self, user_id: int) -> bool:
        return any(user.user_id == user_id for user in self.claimed_telegram_users)

    def claim_telegram_user(self, user_id: int, chat_id: int, display_name: str) -> None:
        if self.status != TelegramTableState.WAITING:
            raise ValueError("Only waiting tables can accept new Telegram users")
        if self.is_full():
            raise ValueError("No open Telegram seats remain")
        if self.has_user(user_id):
            raise ValueError("User is already seated at this table")
        self.claimed_telegram_users.append(
            TelegramUserReservation(
                user_id=user_id,
                chat_id=chat_id,
                display_name=display_name,
            )
        )

    def remove_telegram_user(self, user_id: int) -> TelegramUserReservation | None:
        for index, user in enumerate(self.claimed_telegram_users):
            if user.user_id == user_id:
                return self.claimed_telegram_users.pop(index)
        return None

    def status_view(self) -> TelegramTableStatus:
        return TelegramTableStatus(
            table_id=self.table_id,
            status=self.status,
            creator_user_id=self.creator_user_id,
            total_seats=self.total_seats,
            telegram_seats_total=self.telegram_seat_count,
            telegram_seats_claimed=len(self.claimed_telegram_users),
            llm_seat_count=self.llm_seat_count,
            joined_user_ids=tuple(user.user_id for user in self.claimed_telegram_users),
        )

    def human_users(self) -> tuple[TelegramUserReservation, ...]:
        return tuple(self.claimed_telegram_users)
