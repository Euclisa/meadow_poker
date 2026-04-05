from __future__ import annotations

from dataclasses import dataclass
import logging

from poker_bot.players.base import PlayerAgent
from poker_bot.poker.engine import PokerEngine
from poker_bot.table_runner import run_table
from poker_bot.types import (
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
    PlayerAction,
    PlayerUpdate,
    PlayerUpdateType,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _PendingSeatState:
    validation_error: ActionValidationError | None = None


@dataclass(slots=True)
class _CurrentHandTraceState:
    initial_state: HandStateSnapshot
    initial_events: tuple[GameEvent, ...]
    transitions: list[HandTransition]


class GameOrchestrator:
    """Runs repeated hands while remaining agnostic to player-agent type."""

    def __init__(self, engine: PokerEngine, player_agents: dict[str, PlayerAgent]) -> None:
        self.engine = engine
        self.player_agents = player_agents
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

    async def play_hand(self) -> HandRunResult:
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
        for agent in self.player_agents.values():
            await agent.close()

    async def complete_table(self, *, reason: str, hand_number: int) -> None:
        if self.engine.get_phase() == GamePhase.TABLE_COMPLETE or self._stop_requested:
            return
        logger.info("Completing table reason=%s hand_number=%s", reason, hand_number)
        self._stop_requested = True
        self._append_events((
            GameEvent("table_completed", {"reason": reason, "hand_number": hand_number}),
        ))
        await self._deliver_updates(force_table_completed=True)

    async def _run_current_hand(self) -> None:
        while self.engine.get_phase() != GamePhase.TABLE_COMPLETE and not self.engine.is_hand_complete():
            acting_seat = self.engine.get_acting_seat()
            if acting_seat is None:
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
            decision = self.engine.get_decision_request(
                acting_seat,
                validation_error=pending_error,
            )
            try:
                action = await agent.request_action(decision)
            except Exception as exc:
                legal_types = {la.action_type for la in decision.legal_actions}
                default_type = ActionType.CHECK if ActionType.CHECK in legal_types else ActionType.FOLD
                action = PlayerAction(action_type=default_type)
                logger.warning(
                    "Bot error acting_seat=%s default_action=%s exc=%r",
                    acting_seat,
                    default_type,
                    exc,
                )
            logger.debug("Received action acting_seat=%s action=%s", acting_seat, action)
            self.last_seen_event_index[acting_seat] = len(self.event_log)
            self._pending[acting_seat].validation_error = None

            result = self.engine.apply_action(acting_seat, action, auto_resolve=False)
            logger.debug("Engine apply_action result=%s events=%s", result, result.events)
            if not result.ok:
                if result.events or result.state_changed or self.engine.get_phase() == GamePhase.TABLE_COMPLETE:
                    self._append_events(result.events)
                    await self._deliver_updates()
                    logger.info("Hand ended mid-action result=%s", result)
                    return
                self._pending[acting_seat].validation_error = result.error
                logger.warning("Invalid action from seat=%s error=%s", acting_seat, result.error)
                continue
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
