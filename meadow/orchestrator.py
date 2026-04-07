from __future__ import annotations

import asyncio
from dataclasses import dataclass, replace
import logging
import time
from typing import Awaitable, Callable, Sequence

from meadow.player_agent import PlayerAgent
from meadow.poker.engine import PokerEngine
from meadow.table_runner import run_table
from meadow.types import (
    ActionResult,
    ActionType,
    ActionValidationError,
    GameEvent,
    GamePhase,
    HandArchive,
    HandRecord,
    HandRecordStatus,
    HandRunResult,
    HandStateSnapshot,
    HandTrace,
    HandTransition,
    LegalAction,
    PlayerAction,
    DecisionRequest,
    PlayerUpdate,
    PlayerUpdateType,
)

logger = logging.getLogger(__name__)


class IdleTableTimeoutError(asyncio.TimeoutError):
    pass


@dataclass(slots=True)
class _PendingSeatState:
    validation_error: ActionValidationError | None = None


@dataclass(slots=True)
class _CurrentHandTraceState:
    initial_state: HandStateSnapshot
    initial_events: tuple[GameEvent, ...]
    transitions: list[HandTransition]


@dataclass(frozen=True, slots=True)
class ActiveTurnTimer:
    seat_id: str
    started_monotonic: float
    started_epoch_ms: int
    duration_seconds: int

    @property
    def deadline_monotonic(self) -> float:
        return self.started_monotonic + self.duration_seconds

    @property
    def deadline_epoch_ms(self) -> int:
        return self.started_epoch_ms + (self.duration_seconds * 1000)


def resolve_fallback_action(legal_actions: Sequence[LegalAction]) -> PlayerAction:
    legal_types = {action.action_type for action in legal_actions}
    if ActionType.CHECK in legal_types:
        return PlayerAction(action_type=ActionType.CHECK)
    if ActionType.FOLD in legal_types:
        return PlayerAction(action_type=ActionType.FOLD)
    if legal_actions:
        first = legal_actions[0]
        return PlayerAction(action_type=first.action_type, amount=first.min_amount)
    raise ValueError("Cannot resolve fallback action without any legal actions")


class GameOrchestrator:
    """Runs repeated hands while remaining agnostic to player-agent type."""

    def __init__(
        self,
        engine: PokerEngine,
        player_agents: dict[str, PlayerAgent],
        *,
        turn_timeout_seconds: int | None = None,
        idle_close_seconds: int | None = None,
        on_turn_state_changed: Callable[[], Awaitable[None]] | None = None,
        on_turn_timeout: Callable[[DecisionRequest, PlayerAction, bool], Awaitable[None]] | None = None,
    ) -> None:
        if turn_timeout_seconds is not None and turn_timeout_seconds <= 0:
            raise ValueError("turn_timeout_seconds must be positive when set")
        if idle_close_seconds is not None and idle_close_seconds <= 0:
            raise ValueError("idle_close_seconds must be positive when set")
        self.engine = engine
        self.player_agents = player_agents
        self.turn_timeout_seconds = turn_timeout_seconds
        self.idle_close_seconds = idle_close_seconds
        self._on_turn_state_changed = on_turn_state_changed
        self._on_turn_timeout = on_turn_timeout
        self.event_log: list[GameEvent] = []
        self.completed_hand_archives: list[HandArchive] = []
        self.last_seen_event_index = {seat_id: 0 for seat_id in player_agents}
        self._pending = {seat_id: _PendingSeatState() for seat_id in player_agents}
        self._stop_requested = False
        self._current_hand_event_index: int | None = None
        self._current_hand_start_view = None
        self._current_hand_number: int | None = None
        self._current_hand_ended_in_showdown = False
        self._current_hand_trace: _CurrentHandTraceState | None = None
        self._turn_timer: ActiveTurnTimer | None = None
        self._last_keepalive_activity_monotonic: float | None = None
        self._waiting_for_players = False
        self._participation_changed = asyncio.Event()

        for seat_id in player_agents:
            engine.get_player_view(seat_id)

    def stop(self) -> None:
        self._stop_requested = True

    async def run(self, max_hands: int | None = None, close_agents: bool = True) -> None:
        await run_table(self, max_hands=max_hands, close_agents=close_agents)

    @property
    def completed_hands(self) -> list[HandRecord]:
        return [archive.record for archive in self.completed_hand_archives]

    @property
    def current_hand_record(self) -> HandRecord | None:
        if (
            self._current_hand_event_index is None
            or self._current_hand_start_view is None
            or self._current_hand_number is None
        ):
            return None
        return HandRecord(
            hand_number=self._current_hand_number,
            status=HandRecordStatus.IN_PROGRESS,
            events=tuple(self.event_log[self._current_hand_event_index :]),
            start_public_view=self._current_hand_start_view,
            current_public_view=self.engine.get_public_table_view(),
            ended_in_showdown=self._current_hand_ended_in_showdown,
        )

    @property
    def current_turn_timer(self) -> ActiveTurnTimer | None:
        return self._turn_timer

    async def play_hand(self) -> HandRunResult:
        while True:
            if self._stop_requested:
                return HandRunResult(
                    started=False,
                    hand_number=None,
                    ended_in_showdown=False,
                    table_complete=self.engine.get_phase() == GamePhase.TABLE_COMPLETE,
                )

            logger.info("Starting next hand event_index=%s", len(self.event_log))
            start_index = len(self.event_log)
            start_result = self.engine.start_next_hand(auto_resolve=False)
            logger.debug("start_next_hand result=%s events=%s", start_result, start_result.events)
            if not start_result.ok:
                if start_result.error is not None and start_result.error.code == "waiting_for_players":
                    resumed = await self._pause_until_players_ready()
                    if not resumed:
                        return HandRunResult(
                            started=False,
                            hand_number=None,
                            ended_in_showdown=False,
                            table_complete=self.engine.get_phase() == GamePhase.TABLE_COMPLETE,
                        )
                    continue
                self._append_events(start_result.events)
                await self._deliver_updates()
                logger.info("Table ended: start_next_hand returned ok=False")
                return HandRunResult(
                    started=False,
                    hand_number=None,
                    ended_in_showdown=False,
                    table_complete=self.engine.get_phase() == GamePhase.TABLE_COMPLETE,
                    events=tuple(self.event_log[start_index:]),
                )
            self._begin_current_hand(start_index, initial_events=start_result.events)
            self._initialize_keepalive_deadline_if_needed()
            opening_events = list(start_result.events)
            opening_events.extend(self._record_automatic_progress())
            self._append_events(tuple(opening_events))
            await self._deliver_updates()

            await self._run_current_hand()
            hand_events = tuple(self.event_log[start_index:])
            hand_started = next(
                (event for event in hand_events if event.event_type == "hand_started"),
                None,
            )
            hand_number = hand_started.payload["hand_number"] if hand_started is not None else None
            completed_archive = self._finalize_current_hand()
            completed_hand = completed_archive.record if completed_archive is not None else None
            if completed_hand is not None:
                for seat_id, agent in self.player_agents.items():
                    player_view = self.engine.get_player_view(seat_id)
                    await agent.on_hand_completed(completed_hand, player_view)
            return HandRunResult(
                started=True,
                hand_number=hand_number,
                ended_in_showdown=any(event.event_type == "showdown_started" for event in hand_events),
                table_complete=self.engine.get_phase() == GamePhase.TABLE_COMPLETE,
                events=hand_events,
                completed_hand=completed_hand,
            )

    async def close(self) -> None:
        await self._clear_turn_timer()
        for agent in self.player_agents.values():
            await agent.close()

    async def complete_table(self, *, reason: str, hand_number: int) -> None:
        if self.engine.get_phase() == GamePhase.TABLE_COMPLETE or self._stop_requested:
            return
        logger.info("Completing table reason=%s hand_number=%s", reason, hand_number)
        self._stop_requested = True
        self._participation_changed.set()
        await self._clear_turn_timer()
        self._append_events((
            GameEvent("table_completed", {"reason": reason, "hand_number": hand_number}),
        ))
        await self._deliver_updates(force_table_completed=True)

    async def sit_out_seat(self, seat_id: str, *, reason: str) -> ActionResult:
        acting_before = self.engine.get_acting_seat()
        result = self.engine.sit_out_seat(seat_id, reason=reason, auto_resolve=False)
        if not result.ok:
            return result
        acting_after = self.engine.get_acting_seat()
        if self._turn_timer is not None and self._turn_timer.seat_id == acting_before:
            await self._clear_turn_timer()
        if acting_before is not None and acting_before != acting_after:
            self._cancel_pending_agent(acting_before, reason=reason)
        elif seat_id == acting_before:
            self._cancel_pending_agent(seat_id, reason=reason)
        self._record_participation_transition(result.events)
        events = list(result.events)
        events.extend(self._record_automatic_progress())
        self._append_events(tuple(events))
        await self._deliver_updates()
        await self._sync_pause_state_after_participation_change()
        self._participation_changed.set()
        return ActionResult(ok=True, events=tuple(events), state_changed=True)

    async def sit_in_seat(self, seat_id: str, *, reason: str) -> ActionResult:
        result = self.engine.sit_in_seat(seat_id, reason=reason)
        if not result.ok:
            return result
        self._record_participation_transition(result.events)
        self._append_events(result.events)
        await self._deliver_updates()
        await self._sync_pause_state_after_participation_change()
        self._participation_changed.set()
        return result

    async def _run_current_hand(self) -> None:
        while self.engine.get_phase() != GamePhase.TABLE_COMPLETE and not self.engine.is_hand_complete():
            acting_seat = self.engine.get_acting_seat()
            if acting_seat is None:
                if self.engine.has_pending_automatic_progress():
                    automatic_events = self._record_automatic_progress()
                    if automatic_events:
                        self._append_events(automatic_events)
                        await self._deliver_updates()
                    continue
                if self.engine.get_phase() in {GamePhase.HAND_COMPLETE, GamePhase.TABLE_COMPLETE}:
                    return
                raise RuntimeError("Engine has no acting seat while the hand is still active")

            agent = self.player_agents[acting_seat]
            pending_error = self._pending[acting_seat].validation_error
            logger.debug(
                "Requesting action acting_seat=%s phase=%s pending_error=%s",
                acting_seat,
                self.engine.get_phase(),
                pending_error,
            )
            decision = replace(
                self.engine.get_decision_request(
                    acting_seat,
                    validation_error=pending_error,
                ),
                turn_timeout_seconds=self.turn_timeout_seconds,
            )
            await self._ensure_turn_timer(acting_seat)
            try:
                action = await self._request_action(agent, decision)
            except IdleTableTimeoutError:
                await self.complete_table(
                    reason="idle_timeout",
                    hand_number=decision.public_table_view.hand_number,
                )
                return
            except asyncio.CancelledError as exc:
                if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                    raise
                if self.engine.get_acting_seat() != acting_seat:
                    logger.info("Pending action cancelled after external state change acting_seat=%s", acting_seat)
                    await self._clear_turn_timer()
                    continue
                action = resolve_fallback_action(decision.legal_actions)
                logger.warning(
                    "Pending action cancelled acting_seat=%s fallback_action=%s exc=%r",
                    acting_seat,
                    action,
                    exc,
                )
            except asyncio.TimeoutError:
                if agent.auto_sit_out_on_timeout:
                    logger.warning(
                        "Turn timed out acting_seat=%s timeout=%s action=sit_out",
                        acting_seat,
                        self.turn_timeout_seconds,
                    )
                    await self.sit_out_seat(acting_seat, reason="turn_timeout")
                    if self._on_turn_timeout is not None:
                        await self._on_turn_timeout(
                            decision,
                            PlayerAction(ActionType.FOLD),
                            True,
                        )
                    continue
                action = resolve_fallback_action(decision.legal_actions)
                logger.warning(
                    "Turn timed out acting_seat=%s timeout=%s fallback_action=%s",
                    acting_seat,
                    self.turn_timeout_seconds,
                    action,
                )
                if self._on_turn_timeout is not None:
                    await self._on_turn_timeout(decision, action, False)
            except Exception as exc:
                action = resolve_fallback_action(decision.legal_actions)
                logger.warning(
                    "Bot error acting_seat=%s fallback_action=%s exc=%r",
                    acting_seat,
                    action,
                    exc,
                )
            logger.debug("Received action acting_seat=%s action=%s", acting_seat, action)
            self.last_seen_event_index[acting_seat] = len(self.event_log)
            self._pending[acting_seat].validation_error = None

            result = self.engine.apply_action(acting_seat, action, auto_resolve=False)
            logger.debug("Engine apply_action result=%s events=%s", result, result.events)
            if not result.ok:
                if result.events or result.state_changed or self.engine.get_phase() == GamePhase.TABLE_COMPLETE:
                    await self._clear_turn_timer()
                    self._append_events(result.events)
                    await self._deliver_updates()
                    logger.info("Hand ended mid-action result=%s", result)
                    return
                self._pending[acting_seat].validation_error = result.error
                logger.warning("Invalid action from seat=%s error=%s", acting_seat, result.error)
                continue
            await self._clear_turn_timer()
            if agent.keeps_table_alive:
                self._last_keepalive_activity_monotonic = time.monotonic()
            self._record_action_transition(acting_seat, action, result.events)
            action_events = list(result.events)
            action_events.extend(self._record_automatic_progress())
            self._append_events(tuple(action_events))
            await self._deliver_updates()

    async def _deliver_updates(self, *, force_table_completed: bool = False) -> None:
        for seat_id, agent in self.player_agents.items():
            unseen_events = self._unseen_events_for(seat_id)
            if not unseen_events:
                continue
            update = self._build_update(seat_id, unseen_events, force_table_completed=force_table_completed)
            logger.debug("Delivering update seat_id=%s update=%s", seat_id, update)
            await agent.notify_update(update)
            self.last_seen_event_index[seat_id] = len(self.event_log)

    def _append_events(self, events: tuple[GameEvent, ...]) -> None:
        if any(event.event_type == "showdown_started" for event in events):
            self._current_hand_ended_in_showdown = True
        self.event_log.extend(events)

    def _unseen_events_for(self, seat_id: str) -> tuple[GameEvent, ...]:
        start = self.last_seen_event_index[seat_id]
        return tuple(self.event_log[start:])

    def _build_update(
        self,
        seat_id: str,
        events: tuple[GameEvent, ...],
        *,
        force_table_completed: bool = False,
    ) -> PlayerUpdate:
        player_view = self.engine.get_player_view(seat_id)
        public_view = player_view.public_table
        acting_seat_id = public_view.acting_seat_id
        is_your_turn = acting_seat_id == seat_id
        phase = self.engine.get_phase()
        if force_table_completed or phase == GamePhase.TABLE_COMPLETE:
            update_type = PlayerUpdateType.TABLE_COMPLETED
        elif phase == GamePhase.HAND_COMPLETE:
            update_type = PlayerUpdateType.HAND_COMPLETED
        elif is_your_turn:
            update_type = PlayerUpdateType.TURN_STARTED
        else:
            update_type = PlayerUpdateType.STATE_CHANGED
        return PlayerUpdate(
            update_type=update_type,
            events=events,
            public_table_view=public_view,
            player_view=player_view,
            acting_seat_id=acting_seat_id,
            is_your_turn=is_your_turn,
        )

    def _begin_current_hand(self, event_index: int, *, initial_events: tuple[GameEvent, ...]) -> None:
        public_view = self.engine.get_public_table_view()
        self._current_hand_event_index = event_index
        self._current_hand_start_view = public_view
        self._current_hand_number = public_view.hand_number
        self._current_hand_ended_in_showdown = False
        self._turn_timer = None
        self._current_hand_trace = _CurrentHandTraceState(
            initial_state=self.engine.export_hand_state_snapshot(),
            initial_events=initial_events,
            transitions=[],
        )

    def _finalize_current_hand(self) -> HandArchive | None:
        if (
            self._current_hand_event_index is None
            or self._current_hand_start_view is None
            or self._current_hand_number is None
            or self._current_hand_trace is None
        ):
            return None
        record = HandRecord(
            hand_number=self._current_hand_number,
            status=HandRecordStatus.COMPLETED,
            events=tuple(self.event_log[self._current_hand_event_index :]),
            start_public_view=self._current_hand_start_view,
            current_public_view=self.engine.get_public_table_view(),
            ended_in_showdown=self._current_hand_ended_in_showdown,
        )
        trace = HandTrace(
            hand_number=record.hand_number,
            initial_state=self._current_hand_trace.initial_state,
            initial_events=self._current_hand_trace.initial_events,
            transitions=tuple(self._current_hand_trace.transitions),
            final_state=self.engine.export_hand_state_snapshot(),
            ended_in_showdown=record.ended_in_showdown,
        )
        archive = HandArchive(record=record, trace=trace)
        self.completed_hand_archives.append(archive)
        self._current_hand_event_index = None
        self._current_hand_start_view = None
        self._current_hand_number = None
        self._current_hand_ended_in_showdown = False
        self._current_hand_trace = None
        self._turn_timer = None
        return archive

    def _record_action_transition(
        self,
        seat_id: str,
        action: PlayerAction,
        events: tuple[GameEvent, ...],
    ) -> None:
        if self._current_hand_trace is None:
            return
        self._current_hand_trace.transitions.append(
            HandTransition(
                kind="action",
                seat_id=seat_id,
                action=action,
                events=events,
            )
        )

    def _record_automatic_progress(self) -> tuple[GameEvent, ...]:
        if self._current_hand_trace is None:
            return self.engine.drain_automatic_progress()

        recorded_events: list[GameEvent] = []
        while True:
            progress = self.engine.resolve_automatic_step()
            if not progress.advanced:
                break
            self._current_hand_trace.transitions.append(
                HandTransition(
                    kind="automatic",
                    events=progress.events,
                )
            )
            recorded_events.extend(progress.events)
        return tuple(recorded_events)

    def _record_participation_transition(self, events: tuple[GameEvent, ...]) -> None:
        if self._current_hand_trace is None or not events:
            return
        self._current_hand_trace.transitions.append(
            HandTransition(
                kind="automatic",
                events=events,
            )
        )

    async def _request_action(self, agent: PlayerAgent, decision: DecisionRequest) -> PlayerAction:
        if self.turn_timeout_seconds is None and self.idle_close_seconds is None:
            return await agent.request_action(decision)
        turn_remaining = self._remaining_turn_seconds()
        idle_remaining = self._remaining_keepalive_seconds()
        if idle_remaining is not None and idle_remaining <= 0:
            raise IdleTableTimeoutError
        if turn_remaining is not None and turn_remaining <= 0:
            raise asyncio.TimeoutError
        timeout_candidates = [remaining for remaining in (turn_remaining, idle_remaining) if remaining is not None]
        if not timeout_candidates:
            return await agent.request_action(decision)
        try:
            return await asyncio.wait_for(agent.request_action(decision), timeout=min(timeout_candidates))
        except asyncio.TimeoutError as exc:
            idle_remaining = self._remaining_keepalive_seconds()
            if idle_remaining is not None and idle_remaining <= 0:
                raise IdleTableTimeoutError from exc
            raise

    async def _ensure_turn_timer(self, seat_id: str) -> None:
        if self.turn_timeout_seconds is None:
            return
        if self._turn_timer is not None and self._turn_timer.seat_id == seat_id:
            return
        self._turn_timer = ActiveTurnTimer(
            seat_id=seat_id,
            started_monotonic=time.monotonic(),
            started_epoch_ms=int(time.time() * 1000),
            duration_seconds=self.turn_timeout_seconds,
        )
        await self._notify_turn_state_changed()

    async def _clear_turn_timer(self) -> None:
        if self._turn_timer is None:
            return
        self._turn_timer = None
        await self._notify_turn_state_changed()

    async def _notify_turn_state_changed(self) -> None:
        if self._on_turn_state_changed is not None:
            await self._on_turn_state_changed()

    def _remaining_turn_seconds(self) -> float | None:
        if self._turn_timer is None:
            return None
        return max(0.0, self._turn_timer.deadline_monotonic - time.monotonic())

    def _remaining_keepalive_seconds(self) -> float | None:
        if self.idle_close_seconds is None or self._last_keepalive_activity_monotonic is None:
            return None
        return max(0.0, (self._last_keepalive_activity_monotonic + self.idle_close_seconds) - time.monotonic())

    def _initialize_keepalive_deadline_if_needed(self) -> None:
        if self.idle_close_seconds is None or self._last_keepalive_activity_monotonic is not None:
            return
        if any(agent.keeps_table_alive for agent in self.player_agents.values()):
            self._last_keepalive_activity_monotonic = time.monotonic()

    async def _pause_until_players_ready(self) -> bool:
        await self._enter_waiting_for_players()
        while not self._stop_requested and not self.engine.can_start_next_hand():
            self._participation_changed.clear()
            await self._participation_changed.wait()
        if self._stop_requested:
            return False
        self.engine.refresh_table_readiness()
        await self._leave_waiting_for_players()
        return True

    async def _enter_waiting_for_players(self) -> None:
        if self._waiting_for_players:
            return
        self._waiting_for_players = True
        self._append_events((GameEvent("table_paused", {"reason": "waiting_for_players"}),))
        await self._deliver_updates()

    async def _leave_waiting_for_players(self) -> None:
        if not self._waiting_for_players:
            return
        self._waiting_for_players = False
        self._append_events((GameEvent("table_resumed", {"reason": "players_ready"}),))
        await self._deliver_updates()

    async def _sync_pause_state_after_participation_change(self) -> None:
        if self.engine.is_hand_complete() and self._current_hand_trace is not None:
            return
        phase = self.engine.get_phase()
        if phase in {
            GamePhase.PREFLOP,
            GamePhase.FLOP,
            GamePhase.TURN,
            GamePhase.RIVER,
            GamePhase.SHOWDOWN,
        }:
            return
        if self.engine.can_start_next_hand():
            self.engine.refresh_table_readiness()
            await self._leave_waiting_for_players()
            return
        if self.engine.is_table_active():
            self.engine.refresh_table_readiness()
            await self._enter_waiting_for_players()

    def _cancel_pending_agent(self, seat_id: str, *, reason: str) -> None:
        agent = self.player_agents.get(seat_id)
        if agent is None:
            return
        cancel_pending = getattr(agent, "cancel_pending", None)
        if cancel_pending is None:
            return
        cancel_pending(reason=reason)
