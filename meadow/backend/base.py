from __future__ import annotations

from typing import Any, Protocol

from meadow.backend.models import ActorRef, ManagedTableConfig
from meadow.types import PlayerAction


class TableBackend(Protocol):
    async def list_waiting_tables(self) -> dict[str, Any]:
        raise NotImplementedError

    async def wait_for_waiting_tables_version(
        self,
        after_version: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def create_table(self, actor: ActorRef, table_config: ManagedTableConfig) -> dict[str, Any]:
        raise NotImplementedError

    async def join_table(self, actor: ActorRef, table_id: str) -> dict[str, Any]:
        raise NotImplementedError

    async def start_table(self, actor: ActorRef, table_id: str, viewer_token: str) -> dict[str, Any]:
        raise NotImplementedError

    async def leave_table(self, actor: ActorRef, table_id: str, viewer_token: str) -> dict[str, Any]:
        raise NotImplementedError

    async def cancel_table(self, actor: ActorRef, table_id: str, viewer_token: str) -> dict[str, Any]:
        raise NotImplementedError

    async def get_table_snapshot(self, table_id: str, viewer_token: str | None) -> dict[str, Any]:
        raise NotImplementedError

    async def wait_for_table_version(
        self,
        table_id: str,
        viewer_token: str | None,
        after_version: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def submit_action(self, table_id: str, viewer_token: str, action: PlayerAction) -> dict[str, Any]:
        raise NotImplementedError

    async def request_coach(self, table_id: str, viewer_token: str, question: str) -> dict[str, Any]:
        raise NotImplementedError

    async def get_replay_snapshot(
        self,
        table_id: str,
        viewer_token: str | None,
        hand_number: int,
        step_index: int,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def request_replay_coach(
        self,
        table_id: str,
        viewer_token: str,
        hand_number: int,
        step_index: int,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def get_actor_tables(self, actor: ActorRef) -> dict[str, Any]:
        raise NotImplementedError
