from __future__ import annotations

import secrets

from poker_bot.telegram_app.session import TelegramTableSession
from poker_bot.types import TelegramTableCreateRequest, TelegramTableState


class TelegramTableRegistry:
    def __init__(self) -> None:
        self._tables: dict[str, TelegramTableSession] = {}
        self._user_table_ids: dict[int, str] = {}

    def create_waiting_table(
        self,
        *,
        creator_user_id: int,
        creator_chat_id: int,
        creator_name: str,
        request: TelegramTableCreateRequest,
    ) -> TelegramTableSession:
        if creator_user_id in self._user_table_ids:
            raise ValueError("User is already in a table")

        table_id = self._generate_table_id()
        session = TelegramTableSession(
            table_id=table_id,
            creator_user_id=creator_user_id,
            creator_chat_id=creator_chat_id,
            request=request,
            claimed_telegram_users=[],
        )
        session.claim_telegram_user(
            user_id=creator_user_id,
            chat_id=creator_chat_id,
            display_name=creator_name,
        )
        self._tables[table_id] = session
        self._user_table_ids[creator_user_id] = table_id
        return session

    def get_table(self, table_id: str) -> TelegramTableSession | None:
        return self._tables.get(table_id)

    def get_user_table(self, user_id: int) -> TelegramTableSession | None:
        table_id = self._user_table_ids.get(user_id)
        if table_id is None:
            return None
        return self._tables.get(table_id)

    def join_table(
        self,
        *,
        table_id: str,
        user_id: int,
        chat_id: int,
        display_name: str,
    ) -> TelegramTableSession:
        if user_id in self._user_table_ids:
            raise ValueError("User is already in a table")

        session = self._require_table(table_id)
        if session.status != TelegramTableState.WAITING:
            raise ValueError("Only waiting tables can be joined")
        session.claim_telegram_user(user_id=user_id, chat_id=chat_id, display_name=display_name)
        self._user_table_ids[user_id] = table_id
        return session

    def leave_waiting_table(self, user_id: int) -> TelegramTableSession | None:
        session = self.get_user_table(user_id)
        if session is None:
            return None
        if session.status != TelegramTableState.WAITING:
            raise ValueError("Running tables cannot be left gracefully in v1")
        removed = session.remove_telegram_user(user_id)
        if removed is not None:
            self._user_table_ids.pop(user_id, None)
        return session

    def mark_running(self, session: TelegramTableSession) -> None:
        session.status = TelegramTableState.RUNNING

    def mark_completed(self, session: TelegramTableSession) -> None:
        session.status = TelegramTableState.COMPLETED
        self._remove_user_mappings(session)

    def cancel_table(self, table_id: str) -> TelegramTableSession:
        session = self._require_table(table_id)
        session.status = TelegramTableState.CANCELLED
        self._remove_user_mappings(session)
        return session

    def _remove_user_mappings(self, session: TelegramTableSession) -> None:
        for user in session.claimed_telegram_users:
            self._user_table_ids.pop(user.user_id, None)

    def _require_table(self, table_id: str) -> TelegramTableSession:
        table = self._tables.get(table_id)
        if table is None:
            raise KeyError(f"Unknown table id: {table_id}")
        return table

    def _generate_table_id(self) -> str:
        while True:
            table_id = secrets.token_hex(3)
            if table_id not in self._tables:
                return table_id
