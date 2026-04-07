from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import replace
import secrets
from typing import Any, Awaitable, Callable

from meadow.backend.coach_service import BackendCoachService
from meadow.backend.errors import BackendError
from meadow.backend.human_agent import BackendHumanAgent
from meadow.backend.models import (
    ActorRef,
    BackendTableRuntime,
    ManagedTableConfig,
    SeatReservation,
)
from meadow.backend.replay_service import BackendReplayService
from meadow.backend.runtime_manager import BackendRuntimeManager
from meadow.backend.runtime_state import BackendRuntimePublisher
from meadow.backend.serialization import (
    actor_to_dict,
    game_event_to_dict,
    serialize_private_participants,
    serialize_table_snapshot,
    serialize_waiting_tables,
)
from meadow.config import ThoughtLoggingMode
from meadow.llm_bot import LLMGameClient
from meadow.types import (
    PlayerAction,
    TelegramTableState,
)

DEFAULT_HUMAN_TURN_TIMEOUT_SECONDS = 30
MAX_HUMAN_TURN_TIMEOUT_SECONDS = 180
DEFAULT_HUMAN_IDLE_CLOSE_SECONDS = 300
MAX_HUMAN_IDLE_CLOSE_SECONDS = 1800


class LocalTableBackendService:
    def __init__(
        self,
        *,
        llm_client_factory: Callable[[], LLMGameClient] | None = None,
        coach_client_factory: Callable[[], LLMGameClient] | None = None,
        llm_name_allocator: Any | None = None,
        llm_recent_hand_count: int = 5,
        llm_thought_logging: Any = None,
        coach_enabled: bool = False,
        coach_recent_hand_count: int = 5,
        showdown_delay_seconds: float = 0.0,
        on_runtime_published: Callable[[BackendTableRuntime], Awaitable[None]] | None = None,
    ) -> None:
        self._tables: dict[str, BackendTableRuntime] = {}
        self._table_conditions: dict[str, asyncio.Condition] = {}
        self._actor_table_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._publisher = BackendRuntimePublisher(
            table_conditions=self._table_conditions,
            on_runtime_published=on_runtime_published,
        )
        self._runtime_manager = BackendRuntimeManager(
            publisher=self._publisher,
            llm_client_factory=llm_client_factory,
            coach_client_factory=coach_client_factory,
            llm_name_allocator=llm_name_allocator,
            llm_recent_hand_count=llm_recent_hand_count,
            llm_thought_logging=(
                llm_thought_logging if llm_thought_logging is not None else ThoughtLoggingMode.OFF
            ),
            coach_enabled=coach_enabled,
            coach_recent_hand_count=coach_recent_hand_count,
            showdown_delay_seconds=showdown_delay_seconds,
        )
        self._coach_service = BackendCoachService()
        self._replay_service = BackendReplayService()

    async def list_waiting_tables(self) -> dict[str, Any]:
        waiting = tuple(
            runtime
            for runtime in self._tables.values()
            if runtime.status == TelegramTableState.WAITING
        )
        return serialize_waiting_tables(waiting, version=self._publisher.waiting_tables_version)

    async def wait_for_waiting_tables_version(
        self,
        after_version: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        await self._publisher.wait_for_waiting_tables_version(after_version, timeout_ms)
        return {
            "snapshot": await self.list_waiting_tables(),
        }

    async def create_table(self, actor: ActorRef, table_config: ManagedTableConfig) -> dict[str, Any]:
        table_config = self._normalize_table_config(table_config)
        self._validate_table_config(table_config)
        table_id = self._generate_table_id()
        viewer_token = self._generate_viewer_token()
        creator = SeatReservation(
            seat_id=(
                self._allocate_human_seat_id(table_config.human_seat_prefix, 1)
                if table_config.human_seat_count > 0
                else None
            ),
            viewer_token=viewer_token,
            actor=actor,
        )
        runtime = BackendTableRuntime(
            table_id=table_id,
            config=table_config,
            creator_viewer_token=viewer_token,
            reservations=[creator],
        )
        runtime.add_activity(kind="state", text=f"{actor.display_name} created table {table_id}.")
        self._tables[table_id] = runtime
        self._table_conditions[table_id] = asyncio.Condition()
        self._actor_table_ids[actor.actor_key].add(table_id)
        runtime.status_message = self._waiting_message(runtime)
        await self._publisher.publish_runtime_state(runtime, waiting_tables_changed=True)
        return {
            "table_id": table_id,
            "viewer_token": viewer_token,
            "snapshot": serialize_table_snapshot(runtime, viewer_token=viewer_token),
        }

    async def join_table(self, actor: ActorRef, table_id: str) -> dict[str, Any]:
        runtime = self._require_table(table_id)
        if runtime.status != TelegramTableState.WAITING:
            raise BackendError("Only waiting tables can be joined", status=400)
        if runtime.human_seat_count == 0:
            raise BackendError("This table has no human seats to join", status=400)
        if runtime.find_reservation_by_actor(actor) is not None:
            raise BackendError("Actor already has a seat at this table", status=400)
        if runtime.find_seated_reservation_by_name(actor.display_name) is not None:
            raise BackendError("Display name is already taken at this table", status=400)
        if runtime.is_full():
            raise BackendError(f"No open {runtime.human_transport} seats remain", status=400)
        viewer_token = self._generate_viewer_token()
        reservation = SeatReservation(
            seat_id=self._allocate_human_seat_id(runtime.config.human_seat_prefix, runtime.human_player_count + 1),
            viewer_token=viewer_token,
            actor=actor,
        )
        runtime.reservations.append(reservation)
        self._actor_table_ids[actor.actor_key].add(table_id)
        runtime.add_activity(kind="state", text=f"{actor.display_name} joined table {table_id}.")
        runtime.status_message = self._waiting_message(runtime)
        await self._publisher.publish_runtime_state(runtime, waiting_tables_changed=True)
        return {
            "table_id": table_id,
            "viewer_token": viewer_token,
            "snapshot": serialize_table_snapshot(runtime, viewer_token=viewer_token),
        }

    async def start_table(self, actor: ActorRef, table_id: str, viewer_token: str) -> dict[str, Any]:
        runtime = self._require_table(table_id)
        reservation = self._require_reservation(runtime, viewer_token)
        if reservation.actor.actor_key != actor.actor_key:
            raise BackendError("Viewer token does not belong to the requesting actor", status=403)
        if not runtime.is_creator_token(viewer_token):
            raise BackendError("Only the creator can start the table.", status=403)
        if runtime.status != TelegramTableState.WAITING:
            raise BackendError("Only waiting tables can be started.", status=400)
        if runtime.human_seat_count > 0 and not runtime.is_full():
            transport_label = "Telegram" if runtime.human_transport == "telegram" else runtime.human_transport
            raise BackendError(f"All {transport_label} seats must be claimed before starting.", status=400)
        await self._runtime_manager.start_runtime(runtime)
        return {
            "ok": True,
            "snapshot": serialize_table_snapshot(runtime, viewer_token=viewer_token),
        }

    async def leave_table(self, actor: ActorRef, table_id: str, viewer_token: str) -> dict[str, Any]:
        runtime = self._require_table(table_id)
        reservation = self._require_seated_reservation(runtime, viewer_token)
        if reservation.actor.actor_key != actor.actor_key:
            raise BackendError("Viewer token does not belong to the requesting actor", status=403)
        if runtime.status != TelegramTableState.WAITING:
            raise BackendError("Running tables cannot be left gracefully in v1", status=400)
        if runtime.is_creator_token(viewer_token):
            raise BackendError("Creators must cancel the table instead of leaving it", status=400)
        runtime.reservations = [item for item in runtime.reservations if item.viewer_token != viewer_token]
        self._actor_table_ids[actor.actor_key].discard(table_id)
        runtime.add_activity(kind="state", text=f"{actor.display_name} left table {table_id}.")
        runtime.status_message = self._waiting_message(runtime)
        await self._publisher.publish_runtime_state(runtime, waiting_tables_changed=True)
        return {
            "ok": True,
            "snapshot": serialize_table_snapshot(runtime, viewer_token=None),
            "participants": serialize_private_participants(runtime),
        }

    async def cancel_table(self, actor: ActorRef, table_id: str, viewer_token: str) -> dict[str, Any]:
        runtime = self._require_table(table_id)
        reservation = self._require_reservation(runtime, viewer_token)
        if reservation.actor.actor_key != actor.actor_key:
            raise BackendError("Viewer token does not belong to the requesting actor", status=403)
        if not runtime.is_creator_token(viewer_token):
            raise BackendError("Only the creator can cancel the table.", status=403)
        if runtime.status != TelegramTableState.WAITING:
            raise BackendError("Only waiting tables can be cancelled.", status=400)
        runtime.status = TelegramTableState.CANCELLED
        runtime.status_message = f"Table {table_id} was cancelled."
        runtime.add_activity(kind="state", text=runtime.status_message)
        await self._publisher.publish_runtime_state(runtime, waiting_tables_changed=True)
        return {
            "ok": True,
            "snapshot": serialize_table_snapshot(runtime, viewer_token=viewer_token),
        }

    async def get_table_snapshot(self, table_id: str, viewer_token: str | None) -> dict[str, Any]:
        runtime = self._require_table(table_id)
        authorized = self._publisher.authorize_view(runtime, viewer_token)
        return serialize_table_snapshot(runtime, viewer_token=authorized)

    async def wait_for_table_version(
        self,
        table_id: str,
        viewer_token: str | None,
        after_version: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        runtime = self._require_table(table_id)
        authorized = self._publisher.authorize_view(runtime, viewer_token)
        condition = self._table_conditions[table_id]
        if runtime.version <= after_version:
            try:
                async with condition:
                    await asyncio.wait_for(
                        condition.wait_for(lambda: runtime.version > after_version),
                        timeout=max(timeout_ms, 1) / 1000,
                    )
            except TimeoutError:
                pass
        new_events = [
            game_event_to_dict(event)
            for version, events in runtime._versioned_events
            if version > after_version
            for event in events
        ]
        return {
            "snapshot": serialize_table_snapshot(runtime, viewer_token=authorized),
            "new_events": new_events,
        }

    async def submit_action(self, table_id: str, viewer_token: str, action: PlayerAction) -> dict[str, Any]:
        runtime = self._require_table(table_id)
        reservation = self._require_seated_reservation(runtime, viewer_token)
        if runtime.status != TelegramTableState.RUNNING:
            raise BackendError("Actions are only accepted while the table is running.", status=400)
        assert reservation.seat_id is not None
        agent = runtime.human_agents.get(reservation.seat_id)
        if agent is None:
            raise BackendError(f"This seat is not controlled from {runtime.human_transport}.", status=400)
        error = agent.submit_action(action)
        if error is not None:
            return {
                "ok": False,
                "error": {
                    "code": error.code,
                    "message": error.message,
                },
                "snapshot": serialize_table_snapshot(runtime, viewer_token=viewer_token),
            }
        return {"ok": True}

    async def sit_out(self, table_id: str, viewer_token: str) -> dict[str, Any]:
        runtime = self._require_table(table_id)
        reservation = self._require_seated_reservation(runtime, viewer_token)
        if runtime.status != TelegramTableState.RUNNING:
            raise BackendError("Sit out is only available while the table is running.", status=400)
        if reservation.seat_id is None or runtime.orchestrator is None:
            raise BackendError("This seat cannot be managed right now.", status=400)
        result = await runtime.orchestrator.sit_out_seat(reservation.seat_id, reason="manual")
        if not result.ok:
            assert result.error is not None
            raise BackendError(result.error.message, status=400, code=result.error.code)
        return {
            "ok": True,
            "snapshot": serialize_table_snapshot(runtime, viewer_token=viewer_token),
        }

    async def sit_in(self, table_id: str, viewer_token: str) -> dict[str, Any]:
        runtime = self._require_table(table_id)
        reservation = self._require_seated_reservation(runtime, viewer_token)
        if runtime.status != TelegramTableState.RUNNING:
            raise BackendError("Sit in is only available while the table is running.", status=400)
        if reservation.seat_id is None or runtime.orchestrator is None or runtime.engine is None:
            raise BackendError("This seat cannot be managed right now.", status=400)
        player_view = runtime.engine.get_player_view(reservation.seat_id)
        if player_view.stack <= 0:
            raise BackendError("Busted-out seats cannot sit back in.", status=400, code="no_chips")
        result = await runtime.orchestrator.sit_in_seat(reservation.seat_id, reason="manual")
        if not result.ok:
            assert result.error is not None
            raise BackendError(result.error.message, status=400, code=result.error.code)
        return {
            "ok": True,
            "snapshot": serialize_table_snapshot(runtime, viewer_token=viewer_token),
        }

    async def request_coach(self, table_id: str, viewer_token: str, question: str) -> dict[str, Any]:
        runtime = self._require_table(table_id)
        reservation = self._require_seated_reservation(runtime, viewer_token)
        return await self._coach_service.request_coach(
            runtime=runtime,
            reservation=reservation,
            table_id=table_id,
            question=question,
        )

    async def get_replay_snapshot(
        self,
        table_id: str,
        viewer_token: str | None,
        hand_number: int,
        step_index: int,
    ) -> dict[str, Any]:
        runtime = self._require_table(table_id)
        authorized = self._publisher.authorize_view(runtime, viewer_token)
        return await self._replay_service.get_replay_snapshot(
            runtime=runtime,
            viewer_token=authorized,
            table_id=table_id,
            hand_number=hand_number,
            step_index=step_index,
        )

    async def request_replay_coach(
        self,
        table_id: str,
        viewer_token: str,
        hand_number: int,
        step_index: int,
    ) -> dict[str, Any]:
        runtime = self._require_table(table_id)
        reservation = self._require_seated_reservation(runtime, viewer_token)
        return await self._replay_service.request_replay_coach(
            runtime=runtime,
            reservation=reservation,
            table_id=table_id,
            hand_number=hand_number,
            step_index=step_index,
        )

    async def get_actor_tables(self, actor: ActorRef) -> dict[str, Any]:
        table_ids = sorted(self._actor_table_ids.get(actor.actor_key, set()))
        items = []
        for table_id in table_ids:
            runtime = self._tables.get(table_id)
            if runtime is None:
                continue
            reservation = runtime.find_reservation_by_actor(actor)
            if reservation is None:
                continue
            items.append(
                {
                    "table_id": table_id,
                    "status": runtime.status.value,
                    "viewer_token": reservation.viewer_token,
                    "seat_id": reservation.seat_id,
                    "is_joined": reservation.is_seated,
                    "is_sitting_out": self._reservation_is_sitting_out(runtime, reservation),
                    "is_creator": runtime.is_creator_token(reservation.viewer_token),
                    "message": runtime.status_message,
                    "config_summary": {
                        "small_blind": runtime.config.small_blind,
                        "big_blind": runtime.config.big_blind,
                        "ante": runtime.config.ante,
                        "starting_stack": runtime.config.starting_stack,
                        "turn_timeout_seconds": runtime.config.turn_timeout_seconds,
                        "idle_close_seconds": runtime.config.idle_close_seconds,
                    },
                }
            )
        return {
            "actor": actor_to_dict(actor),
            "tables": items,
        }

    def _require_table(self, table_id: str) -> BackendTableRuntime:
        runtime = self._tables.get(table_id)
        if runtime is None:
            raise BackendError("Table not found.", status=404)
        return runtime

    def _require_reservation(self, runtime: BackendTableRuntime, viewer_token: str | None) -> SeatReservation:
        reservation = runtime.find_reservation_by_token(viewer_token)
        if reservation is None:
            raise BackendError("A valid viewer token is required.", status=403)
        return reservation

    def _require_seated_reservation(self, runtime: BackendTableRuntime, viewer_token: str | None) -> SeatReservation:
        reservation = runtime.find_seated_reservation_by_token(viewer_token)
        if reservation is None:
            raise BackendError("A valid seated viewer token is required.", status=403)
        return reservation

    def _validate_table_config(self, config: ManagedTableConfig) -> None:
        if not 2 <= config.total_seats <= config.max_players:
            raise BackendError(f"total_seats must be between 2 and {config.max_players}.", status=400)
        if not 0 <= config.llm_seat_count <= config.total_seats:
            raise BackendError(f"llm_seat_count must be between 0 and {config.total_seats}.", status=400)
        if config.human_seat_count > 0:
            if config.turn_timeout_seconds is None:
                raise BackendError(
                    (
                        "turn_timeout_seconds is required for tables with human seats and must be "
                        f"between 1 and {MAX_HUMAN_TURN_TIMEOUT_SECONDS} seconds."
                    ),
                    status=400,
                )
            if not 1 <= config.turn_timeout_seconds <= MAX_HUMAN_TURN_TIMEOUT_SECONDS:
                raise BackendError(
                    f"turn_timeout_seconds must be between 1 and {MAX_HUMAN_TURN_TIMEOUT_SECONDS} seconds.",
                    status=400,
                )
            if config.idle_close_seconds is None:
                raise BackendError(
                    (
                        "idle_close_seconds is required for tables with human seats and must be "
                        f"between {config.turn_timeout_seconds} and {MAX_HUMAN_IDLE_CLOSE_SECONDS} seconds."
                    ),
                    status=400,
                )
            if not config.turn_timeout_seconds <= config.idle_close_seconds <= MAX_HUMAN_IDLE_CLOSE_SECONDS:
                raise BackendError(
                    (
                        "idle_close_seconds must be at least turn_timeout_seconds and at most "
                        f"{MAX_HUMAN_IDLE_CLOSE_SECONDS} seconds."
                    ),
                    status=400,
                )
            return
        if config.turn_timeout_seconds is not None and config.turn_timeout_seconds <= 0:
            raise BackendError("turn_timeout_seconds must be positive when set.", status=400)
        if config.idle_close_seconds is not None and config.idle_close_seconds <= 0:
            raise BackendError("idle_close_seconds must be positive when set.", status=400)

    def _normalize_table_config(self, config: ManagedTableConfig) -> ManagedTableConfig:
        if config.human_seat_count <= 0 or config.idle_close_seconds is not None:
            return config
        return replace(config, idle_close_seconds=DEFAULT_HUMAN_IDLE_CLOSE_SECONDS)

    def _waiting_message(self, runtime: BackendTableRuntime) -> str:
        if runtime.human_seat_count == 0:
            return f"Table {runtime.table_id} is ready to start."
        if runtime.is_full():
            return f"Table {runtime.table_id} is ready to start."
        remaining = runtime.open_human_seat_count()
        suffix = "" if remaining == 1 else "s"
        return f"Waiting for {remaining} more player{suffix}."

    @staticmethod
    def _generate_table_id() -> str:
        return secrets.token_hex(3)

    @staticmethod
    def _generate_viewer_token() -> str:
        return secrets.token_urlsafe(18)

    @staticmethod
    def _allocate_human_seat_id(prefix: str, index: int) -> str:
        return f"{prefix}_{index}" if prefix not in {"p"} else f"p{index}"

    @staticmethod
    def _reservation_is_sitting_out(runtime: BackendTableRuntime, reservation: SeatReservation) -> bool:
        if runtime.engine is None or reservation.seat_id is None:
            return False
        return any(
            seat.seat_id == reservation.seat_id and seat.is_sitting_out
            for seat in runtime.engine.get_public_table_view().seats
        )


class LocalBackendClient:
    def __init__(self, service: LocalTableBackendService) -> None:
        self._service = service

    async def list_waiting_tables(self) -> dict[str, Any]:
        return await self._service.list_waiting_tables()

    async def wait_for_waiting_tables_version(
        self,
        after_version: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        return await self._service.wait_for_waiting_tables_version(after_version, timeout_ms)

    async def create_table(self, actor: ActorRef, table_config: ManagedTableConfig) -> dict[str, Any]:
        return await self._service.create_table(actor, table_config)

    async def join_table(self, actor: ActorRef, table_id: str) -> dict[str, Any]:
        return await self._service.join_table(actor, table_id)

    async def start_table(self, actor: ActorRef, table_id: str, viewer_token: str) -> dict[str, Any]:
        return await self._service.start_table(actor, table_id, viewer_token)

    async def leave_table(self, actor: ActorRef, table_id: str, viewer_token: str) -> dict[str, Any]:
        return await self._service.leave_table(actor, table_id, viewer_token)

    async def cancel_table(self, actor: ActorRef, table_id: str, viewer_token: str) -> dict[str, Any]:
        return await self._service.cancel_table(actor, table_id, viewer_token)

    async def get_table_snapshot(self, table_id: str, viewer_token: str | None) -> dict[str, Any]:
        return await self._service.get_table_snapshot(table_id, viewer_token)

    async def wait_for_table_version(
        self,
        table_id: str,
        viewer_token: str | None,
        after_version: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        return await self._service.wait_for_table_version(table_id, viewer_token, after_version, timeout_ms)

    async def submit_action(self, table_id: str, viewer_token: str, action: PlayerAction) -> dict[str, Any]:
        return await self._service.submit_action(table_id, viewer_token, action)

    async def sit_out(self, table_id: str, viewer_token: str) -> dict[str, Any]:
        return await self._service.sit_out(table_id, viewer_token)

    async def sit_in(self, table_id: str, viewer_token: str) -> dict[str, Any]:
        return await self._service.sit_in(table_id, viewer_token)

    async def request_coach(self, table_id: str, viewer_token: str, question: str) -> dict[str, Any]:
        return await self._service.request_coach(table_id, viewer_token, question)

    async def get_replay_snapshot(
        self,
        table_id: str,
        viewer_token: str | None,
        hand_number: int,
        step_index: int,
    ) -> dict[str, Any]:
        return await self._service.get_replay_snapshot(table_id, viewer_token, hand_number, step_index)

    async def request_replay_coach(
        self,
        table_id: str,
        viewer_token: str,
        hand_number: int,
        step_index: int,
    ) -> dict[str, Any]:
        return await self._service.request_replay_coach(table_id, viewer_token, hand_number, step_index)

    async def get_actor_tables(self, actor: ActorRef) -> dict[str, Any]:
        return await self._service.get_actor_tables(actor)
