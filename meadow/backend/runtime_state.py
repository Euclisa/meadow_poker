from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from meadow.backend.models import BackendTableRuntime
from meadow.types import GameEvent


class BackendRuntimePublisher:
    def __init__(
        self,
        *,
        table_conditions: dict[str, asyncio.Condition],
        on_runtime_published: Callable[[BackendTableRuntime], Awaitable[None]] | None = None,
    ) -> None:
        self._table_conditions = table_conditions
        self._on_runtime_published = on_runtime_published
        self._waiting_tables_version = 1
        self._waiting_tables_condition = asyncio.Condition()

    @property
    def waiting_tables_version(self) -> int:
        return self._waiting_tables_version

    def authorize_view(self, runtime: BackendTableRuntime, viewer_token: str | None) -> str | None:
        if runtime.find_reservation_by_token(viewer_token) is not None:
            return viewer_token
        return None

    async def wait_for_waiting_tables_version(self, after_version: int, timeout_ms: int) -> None:
        if self._waiting_tables_version <= after_version:
            try:
                async with self._waiting_tables_condition:
                    await asyncio.wait_for(
                        self._waiting_tables_condition.wait_for(lambda: self._waiting_tables_version > after_version),
                        timeout=max(timeout_ms, 1) / 1000,
                    )
            except TimeoutError:
                pass

    async def publish_runtime_state(
        self,
        runtime: BackendTableRuntime,
        *,
        waiting_tables_changed: bool = False,
    ) -> None:
        new_events: tuple[GameEvent, ...] = ()
        orchestrator = runtime.orchestrator
        if orchestrator is not None and len(orchestrator.event_log) > runtime._published_event_index:
            new_events = tuple(orchestrator.event_log[runtime._published_event_index :])
            runtime._published_event_index = len(orchestrator.event_log)
        runtime.version += 1
        runtime._versioned_events.append((runtime.version, new_events))
        condition = self._table_conditions.get(runtime.table_id)
        if condition is not None:
            async with condition:
                condition.notify_all()
        if waiting_tables_changed:
            self._waiting_tables_version += 1
            async with self._waiting_tables_condition:
                self._waiting_tables_condition.notify_all()
        if self._on_runtime_published is not None:
            await self._on_runtime_published(runtime)
