from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass
import logging
import secrets
from typing import Any, Awaitable, Callable

from meadow.backend.models import (
    ActorRef,
    BackendTableRuntime,
    ManagedTableConfig,
    SeatReservation,
    ShowdownReveal,
    ShowdownState,
    ShowdownWinner,
)
from meadow.backend.serialization import (
    actor_to_dict,
    game_event_to_dict,
    serialize_private_participants,
    serialize_replay_snapshot,
    serialize_table_snapshot,
    serialize_waiting_tables,
)
from meadow.coach import CoachRequestError, TableCoach
from meadow.config import ThoughtLoggingMode
from meadow.players.base import PlayerAgent
from meadow.players.llm import LLMGameClient, LLMPlayerAgent
from meadow.poker.engine import PokerEngine
from meadow.replay import HandReplayBuildError, HandReplaySession, ReplayAnalysisError, build_replay_decision_spot
from meadow.table_runner import run_table
from meadow.types import (
    ActionType,
    ActionValidationError,
    DecisionRequest,
    GameEvent,
    PlayerAction,
    PlayerUpdate,
    SeatConfig,
    TableConfig,
    TelegramTableState,
)

logger = logging.getLogger(__name__)


class BackendError(RuntimeError):
    def __init__(self, message: str, *, status: int = 400, code: str = "backend_error") -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


@dataclass(slots=True)
class _PendingState:
    decision_request: DecisionRequest


class BackendHumanAgent(PlayerAgent):
    def __init__(
        self,
        seat_id: str,
        *,
        on_state_changed: Callable[[], Awaitable[None]],
    ) -> None:
        self.seat_id = seat_id
        self._on_state_changed = on_state_changed
        self._pending_state: _PendingState | None = None
        self._pending_future: asyncio.Future[PlayerAction] | None = None

    @property
    def pending_decision(self) -> DecisionRequest | None:
        if self._pending_state is None:
            return None
        return self._pending_state.decision_request

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        if self._pending_future is not None:
            raise RuntimeError("A human action is already pending for this seat")
        self._pending_state = _PendingState(decision_request=decision)
        self._pending_future = asyncio.get_running_loop().create_future()
        await self._on_state_changed()
        try:
            return await self._pending_future
        finally:
            self._pending_future = None
            self._pending_state = None

    async def notify_update(self, update: PlayerUpdate) -> None:
        del update
        self._pending_state = None
        await self._on_state_changed()

    async def close(self) -> None:
        if self._pending_future is not None and not self._pending_future.done():
            self._pending_future.cancel("agent_closed")
        self._pending_future = None
        self._pending_state = None
        await self._on_state_changed()

    def submit_action(self, action: PlayerAction) -> ActionValidationError | None:
        if self._pending_state is None or self._pending_future is None or self._pending_future.done():
            return ActionValidationError("no_pending_action", "There is no pending action for this seat.")
        legal_action = next(
            (
                item
                for item in self._pending_state.decision_request.legal_actions
                if item.action_type == action.action_type
            ),
            None,
        )
        if legal_action is None:
            return ActionValidationError("illegal_action", "That action is not legal right now.")
        if action.action_type in {ActionType.BET, ActionType.RAISE}:
            if action.amount is None:
                return ActionValidationError("missing_amount", "This action requires a total amount.")
            if legal_action.min_amount is not None and action.amount < legal_action.min_amount:
                return ActionValidationError("amount_too_small", f"Amount must be at least {legal_action.min_amount}.")
            if legal_action.max_amount is not None and action.amount > legal_action.max_amount:
                return ActionValidationError("amount_too_large", f"Amount must be at most {legal_action.max_amount}.")
        self._pending_future.set_result(action)
        return None


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
        self._llm_client_factory = llm_client_factory
        self._coach_client_factory = coach_client_factory
        self._llm_name_allocator = llm_name_allocator
        self._llm_recent_hand_count = llm_recent_hand_count
        self._llm_thought_logging = (
            llm_thought_logging if llm_thought_logging is not None else ThoughtLoggingMode.OFF
        )
        self._coach_enabled = coach_enabled
        self._coach_recent_hand_count = coach_recent_hand_count
        self._showdown_delay_seconds = showdown_delay_seconds
        self._on_runtime_published = on_runtime_published
        self._tables: dict[str, BackendTableRuntime] = {}
        self._table_conditions: dict[str, asyncio.Condition] = {}
        self._actor_table_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
        self._waiting_tables_version = 1
        self._waiting_tables_condition = asyncio.Condition()

    async def list_waiting_tables(self) -> dict[str, Any]:
        waiting = tuple(
            runtime
            for runtime in self._tables.values()
            if runtime.status == TelegramTableState.WAITING
        )
        return serialize_waiting_tables(waiting, version=self._waiting_tables_version)

    async def wait_for_waiting_tables_version(
        self,
        after_version: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        if self._waiting_tables_version <= after_version:
            try:
                async with self._waiting_tables_condition:
                    await asyncio.wait_for(
                        self._waiting_tables_condition.wait_for(lambda: self._waiting_tables_version > after_version),
                        timeout=max(timeout_ms, 1) / 1000,
                    )
            except TimeoutError:
                pass
        return {
            "snapshot": await self.list_waiting_tables(),
        }

    async def create_table(self, actor: ActorRef, table_config: ManagedTableConfig) -> dict[str, Any]:
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
        await self._publish_runtime_state(runtime, waiting_tables_changed=True)
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
        await self._publish_runtime_state(runtime, waiting_tables_changed=True)
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
        await self._start_runtime(runtime)
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
        await self._publish_runtime_state(runtime, waiting_tables_changed=True)
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
        await self._publish_runtime_state(runtime, waiting_tables_changed=True)
        return {
            "ok": True,
            "snapshot": serialize_table_snapshot(runtime, viewer_token=viewer_token),
        }

    async def get_table_snapshot(self, table_id: str, viewer_token: str | None) -> dict[str, Any]:
        runtime = self._require_table(table_id)
        authorized = self._authorize_view(runtime, viewer_token)
        return serialize_table_snapshot(runtime, viewer_token=authorized)

    async def wait_for_table_version(
        self,
        table_id: str,
        viewer_token: str | None,
        after_version: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        runtime = self._require_table(table_id)
        authorized = self._authorize_view(runtime, viewer_token)
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

    async def request_coach(self, table_id: str, viewer_token: str, question: str) -> dict[str, Any]:
        runtime = self._require_table(table_id)
        reservation = self._require_seated_reservation(runtime, viewer_token)
        if runtime.status != TelegramTableState.RUNNING:
            raise BackendError("Coach tips are only available while the table is running.", status=400)
        if runtime.coach is None:
            raise BackendError("Coach is not enabled for this table.", status=400)
        assert reservation.seat_id is not None
        agent = runtime.human_agents.get(reservation.seat_id)
        if agent is None or agent.pending_decision is None:
            raise BackendError("Coach tips are only available on your turn.", status=400)
        orchestrator = runtime.orchestrator
        if orchestrator is None or orchestrator.current_hand_record is None:
            raise BackendError("Current hand context is unavailable.", status=400)
        try:
            reply = await runtime.coach.answer_question(
                table_id=table_id,
                seat_id=reservation.seat_id,
                decision=agent.pending_decision,
                current_hand_record=orchestrator.current_hand_record,
                question=question,
            )
        except CoachRequestError as exc:
            raise BackendError(str(exc), status=504) from exc
        return {"ok": True, "reply": reply}

    async def get_replay_snapshot(
        self,
        table_id: str,
        viewer_token: str | None,
        hand_number: int,
        step_index: int,
    ) -> dict[str, Any]:
        runtime = self._require_table(table_id)
        authorized = self._authorize_view(runtime, viewer_token)
        archive = runtime.find_completed_hand_archive(hand_number)
        if archive is None:
            raise BackendError("Completed hand not found.", status=404)
        viewer = runtime.find_reservation_by_token(authorized)
        try:
            replay_session = HandReplaySession(
                archive.trace,
                viewer_seat_id=viewer.seat_id if viewer is not None else None,
            )
            frame = replay_session.materialize(step_index)
        except HandReplayBuildError as exc:
            logger.warning("Replay build failed table=%s hand=%s error=%s", table_id, hand_number, exc)
            raise BackendError("Replay could not be built for this hand.", status=500) from exc
        except IndexError as exc:
            raise BackendError("Replay step is out of range.", status=400) from exc
        return serialize_replay_snapshot(runtime, archive, frame, viewer_token=authorized)

    async def request_replay_coach(
        self,
        table_id: str,
        viewer_token: str,
        hand_number: int,
        step_index: int,
    ) -> dict[str, Any]:
        from meadow.hand_history import render_replay_public_hand_summary

        runtime = self._require_table(table_id)
        reservation = self._require_seated_reservation(runtime, viewer_token)
        if runtime.coach is None:
            raise BackendError("Coach is not enabled for this table.", status=400)
        assert reservation.seat_id is not None
        archive = runtime.find_completed_hand_archive(hand_number)
        if archive is None:
            raise BackendError("Completed hand not found.", status=404)
        try:
            spot = build_replay_decision_spot(
                archive.trace,
                step_index=step_index,
                viewer_seat_id=reservation.seat_id,
            )
        except HandReplayBuildError as exc:
            raise BackendError("Replay could not be built for this hand.", status=500) from exc
        except IndexError as exc:
            raise BackendError("Replay step is out of range.", status=400) from exc
        except ReplayAnalysisError as exc:
            raise BackendError(str(exc), status=400) from exc
        replay_hand_summary = render_replay_public_hand_summary(
            hand_number=archive.record.hand_number,
            events=spot.frame.visible_events,
            start_public_view=archive.record.start_public_view,
            current_public_view=spot.frame.public_table_view,
        )
        try:
            reply = await runtime.coach.analyze_replay_spot(
                table_id=table_id,
                seat_id=reservation.seat_id,
                decision=spot.decision,
                replay_hand_summary=replay_hand_summary,
                next_transition=spot.next_transition,
                replay_hand_number=archive.record.hand_number,
            )
        except CoachRequestError as exc:
            raise BackendError(str(exc), status=504) from exc
        return {"ok": True, "reply": reply}

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
                    "is_creator": runtime.is_creator_token(reservation.viewer_token),
                    "message": runtime.status_message,
                    "config_summary": {
                        "small_blind": runtime.config.small_blind,
                        "big_blind": runtime.config.big_blind,
                        "ante": runtime.config.ante,
                        "starting_stack": runtime.config.starting_stack,
                        "turn_timeout_seconds": runtime.config.turn_timeout_seconds,
                    },
                }
            )
        return {
            "actor": actor_to_dict(actor),
            "tables": items,
        }

    async def _start_runtime(self, runtime: BackendTableRuntime) -> None:
        seat_configs: list[SeatConfig] = []
        player_agents: dict[str, PlayerAgent] = {}

        async def publish_state() -> None:
            await self._publish_runtime_state(runtime)

        async def handle_turn_timeout(decision: DecisionRequest, action: PlayerAction) -> None:
            runtime.add_activity(
                kind="state",
                text=f"Time expired. Auto-{self._format_action_label(action)}.",
            )
            await self._publish_runtime_state(runtime)

        for reservation in runtime.reservations:
            if not reservation.is_seated or reservation.seat_id is None:
                continue
            seat_configs.append(SeatConfig(seat_id=reservation.seat_id, name=reservation.actor.display_name))
            human_agent = BackendHumanAgent(seat_id=reservation.seat_id, on_state_changed=publish_state)
            player_agents[reservation.seat_id] = human_agent
            runtime.human_agents[reservation.seat_id] = human_agent

        for index in range(1, runtime.llm_seat_count + 1):
            seat_id = f"llm_{index}"
            seat_configs.append(SeatConfig(seat_id=seat_id, name=self._allocate_llm_name()))
            player_agents[seat_id] = LLMPlayerAgent(
                seat_id=seat_id,
                client=self._require_llm_client_factory()(),
                recent_hand_count=self._llm_recent_hand_count,
                thought_logging=self._llm_thought_logging,
            )

        engine = PokerEngine.create_table(
            TableConfig(
                small_blind=runtime.config.small_blind,
                big_blind=runtime.config.big_blind,
                ante=runtime.config.ante,
                starting_stack=runtime.config.starting_stack,
                max_players=runtime.total_seats,
            ),
            seat_configs,
        )
        from meadow.orchestrator import GameOrchestrator

        orchestrator = GameOrchestrator(
            engine,
            player_agents,
            turn_timeout_seconds=runtime.config.turn_timeout_seconds,
            on_turn_state_changed=publish_state,
            on_turn_timeout=handle_turn_timeout,
        )
        runtime.engine = engine
        runtime.player_agents = player_agents
        runtime.orchestrator = orchestrator
        runtime.coach = self._build_table_coach()
        runtime.status = TelegramTableState.RUNNING
        runtime.status_message = f"Table {runtime.table_id} started with {runtime.total_seats} seats."
        runtime.add_activity(kind="state", text=runtime.status_message)
        await self._publish_runtime_state(runtime, waiting_tables_changed=True)
        runtime.orchestrator_task = asyncio.create_task(self._run_runtime(runtime))

    async def _run_runtime(self, runtime: BackendTableRuntime) -> None:
        assert runtime.orchestrator is not None

        async def after_hand(result: Any) -> None:
            if result.completed_hand is not None and runtime.coach is not None:
                await runtime.coach.record_completed_hand(result.completed_hand)
            if not result.ended_in_showdown:
                return
            runtime.showdown_state = self._build_showdown_state(result)
            await self._publish_runtime_state(runtime)
            if self._showdown_delay_seconds > 0:
                await asyncio.sleep(self._showdown_delay_seconds)
                runtime.showdown_state = None
                await self._publish_runtime_state(runtime)

        try:
            await run_table(
                runtime.orchestrator,
                max_hands=runtime.config.max_hands_per_table,
                close_agents=True,
                after_hand=after_hand,
            )
        finally:
            logger.info("Backend table %s completed", runtime.table_id)
            runtime.status = TelegramTableState.COMPLETED
            runtime.status_message = f"Table {runtime.table_id} has completed."
            runtime.add_activity(kind="state", text=runtime.status_message)
            await self._publish_runtime_state(runtime, waiting_tables_changed=True)

    async def _publish_runtime_state(
        self,
        runtime: BackendTableRuntime,
        *,
        waiting_tables_changed: bool = False,
    ) -> None:
        new_events: tuple[GameEvent, ...] = ()
        orchestrator = runtime.orchestrator
        if orchestrator is not None:
            if len(orchestrator.event_log) > runtime._published_event_index:
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

    def _authorize_view(self, runtime: BackendTableRuntime, viewer_token: str | None) -> str | None:
        if runtime.find_reservation_by_token(viewer_token) is not None:
            return viewer_token
        return None

    def _build_table_coach(self) -> TableCoach | None:
        if not self._coach_enabled:
            return None
        if self._coach_client_factory is None:
            raise RuntimeError("Coach client factory is required when coach is enabled")
        return TableCoach(
            self._coach_client_factory(),
            recent_hand_count=self._coach_recent_hand_count,
        )

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
        if config.turn_timeout_seconds is not None and config.turn_timeout_seconds <= 0:
            raise BackendError("turn_timeout_seconds must be positive when set.", status=400)

    def _waiting_message(self, runtime: BackendTableRuntime) -> str:
        if runtime.human_seat_count == 0:
            return f"Table {runtime.table_id} is ready to start."
        if runtime.is_full():
            return f"Table {runtime.table_id} is ready to start."
        remaining = runtime.open_human_seat_count()
        suffix = "" if remaining == 1 else "s"
        return f"Waiting for {remaining} more player{suffix}."

    def _build_showdown_state(self, result: Any) -> ShowdownState:
        return ShowdownState(
            revealed_seats=tuple(
                ShowdownReveal(
                    seat_id=event.payload["seat_id"],
                    hole_cards=tuple(event.payload["hole_cards"]),
                )
                for event in result.events
                if event.event_type == "showdown_revealed"
            ),
            winners=tuple(
                ShowdownWinner(
                    seat_id=event.payload["seat_id"],
                    amount=event.payload["amount"],
                )
                for event in result.events
                if event.event_type == "pot_awarded"
            ),
        )

    def _allocate_llm_name(self) -> str:
        if self._llm_name_allocator is None:
            from meadow.naming import BotNameAllocator

            self._llm_name_allocator = BotNameAllocator()
        return self._llm_name_allocator.allocate()

    def _require_llm_client_factory(self) -> Callable[[], LLMGameClient]:
        if self._llm_client_factory is None:
            raise RuntimeError("LLM client factory is required to create LLM seats")
        return self._llm_client_factory

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
    def _format_action_label(action: PlayerAction) -> str:
        if action.amount is None:
            return action.action_type.value
        return f"{action.action_type.value} {action.amount}"


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
