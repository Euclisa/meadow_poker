from __future__ import annotations

import asyncio
import secrets

from poker_bot.types import TelegramTableState
from poker_bot.web_app.session import WebTableCreateRequest, WebTableSession, WebUserReservation


class WebTableRegistry:
    def __init__(self) -> None:
        self._tables: dict[str, WebTableSession] = {}
        self._lobby_watchers: set[asyncio.Queue[str]] = set()

    def create_waiting_table(
        self,
        *,
        creator_name: str,
        request: WebTableCreateRequest,
    ) -> tuple[WebTableSession, WebUserReservation]:
        table_id = self._generate_table_id()
        creator = WebUserReservation(
            seat_id="web_1",
            seat_token=self._generate_seat_token(),
            display_name=creator_name,
        )
        session = WebTableSession(
            table_id=table_id,
            creator_seat_token=creator.seat_token,
            request=request,
            claimed_web_users=[],
        )
        session.claim_web_user(creator)
        session.add_activity(kind="state", text=f"{creator.display_name} created table {table_id}.")
        self._tables[table_id] = session
        self.notify_lobby_watchers()
        session.notify_watchers()
        return session, creator

    def list_waiting_tables(self) -> tuple[WebTableSession, ...]:
        return tuple(
            session
            for session in self._tables.values()
            if session.status == TelegramTableState.WAITING
        )

    def get_table(self, table_id: str) -> WebTableSession | None:
        return self._tables.get(table_id)

    def join_table(self, *, table_id: str, display_name: str) -> tuple[WebTableSession, WebUserReservation]:
        session = self._require_table(table_id)
        if session.status != TelegramTableState.WAITING:
            raise ValueError("Only waiting tables can be joined")
        if session.find_reservation_by_name(display_name) is not None:
            raise ValueError("Display name is already taken at this table")
        if session.is_full():
            raise ValueError("No open web seats remain")
        reservation = WebUserReservation(
            seat_id=self._allocate_open_web_seat_id(session),
            seat_token=self._generate_seat_token(),
            display_name=display_name,
        )
        session.claim_web_user(reservation)
        session.add_activity(kind="state", text=f"{display_name} joined table {session.table_id}.")
        self.notify_lobby_watchers()
        session.notify_watchers()
        return session, reservation

    def rejoin_table(self, *, table_id: str, seat_token: str) -> tuple[WebTableSession, WebUserReservation]:
        session = self._require_table(table_id)
        reservation = session.find_reservation_by_token(seat_token)
        if reservation is None:
            raise KeyError(f"Unknown seat token for table {table_id}")
        return session, reservation

    def leave_waiting_table(self, *, table_id: str, seat_token: str) -> WebTableSession:
        session = self._require_table(table_id)
        if session.status != TelegramTableState.WAITING:
            raise ValueError("Running tables cannot be left gracefully in v1")
        if session.is_creator_token(seat_token):
            raise ValueError("Creators must cancel the table instead of leaving it")
        removed = session.remove_web_user(seat_token)
        if removed is None:
            raise KeyError(f"Unknown seat token for table {table_id}")
        session.add_activity(kind="state", text=f"{removed.display_name} left table {session.table_id}.")
        self.notify_lobby_watchers()
        session.notify_watchers()
        return session

    def cancel_table(self, table_id: str) -> WebTableSession:
        session = self._require_table(table_id)
        session.status = TelegramTableState.CANCELLED
        self.notify_lobby_watchers()
        session.notify_watchers()
        return session

    def mark_running(self, session: WebTableSession) -> None:
        session.status = TelegramTableState.RUNNING
        self.notify_lobby_watchers()
        session.notify_watchers()

    def mark_completed(self, session: WebTableSession) -> None:
        session.status = TelegramTableState.COMPLETED
        self.notify_lobby_watchers()
        session.notify_watchers()

    def subscribe_lobby(self) -> asyncio.Queue[str]:
        queue: asyncio.Queue[str] = asyncio.Queue()
        self._lobby_watchers.add(queue)
        return queue

    def unsubscribe_lobby(self, queue: asyncio.Queue[str]) -> None:
        self._lobby_watchers.discard(queue)

    def notify_lobby_watchers(self) -> None:
        for queue in list(self._lobby_watchers):
            if queue.qsize() > 1:
                continue
            try:
                queue.put_nowait("update")
            except RuntimeError:
                self._lobby_watchers.discard(queue)

    def _require_table(self, table_id: str) -> WebTableSession:
        table = self._tables.get(table_id)
        if table is None:
            raise KeyError(f"Unknown table id: {table_id}")
        return table

    def _allocate_open_web_seat_id(self, session: WebTableSession) -> str:
        claimed = {user.seat_id for user in session.claimed_web_users}
        for index in range(1, session.web_seat_count + 1):
            seat_id = f"web_{index}"
            if seat_id not in claimed:
                return seat_id
        raise ValueError("No open web seats remain")

    def _generate_table_id(self) -> str:
        while True:
            table_id = secrets.token_hex(3)
            if table_id not in self._tables:
                return table_id

    @staticmethod
    def _generate_seat_token() -> str:
        return secrets.token_urlsafe(18)
