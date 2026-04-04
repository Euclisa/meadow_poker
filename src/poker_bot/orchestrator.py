from __future__ import annotations

from dataclasses import dataclass
import logging

from poker_bot.players.base import PlayerAgent
from poker_bot.poker.engine import PokerEngine
from poker_bot.types import ActionValidationError, GameEvent, GamePhase, PlayerUpdate, PlayerUpdateType

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _PendingSeatState:
    validation_error: ActionValidationError | None = None


class GameOrchestrator:
    """Runs repeated hands while remaining agnostic to player-agent type."""

    def __init__(self, engine: PokerEngine, player_agents: dict[str, PlayerAgent]) -> None:
        self.engine = engine
        self.player_agents = player_agents
        self.event_log: list[GameEvent] = []
        self.last_seen_event_index = {seat_id: 0 for seat_id in player_agents}
        self._pending = {seat_id: _PendingSeatState() for seat_id in player_agents}
        self._stop_requested = False

        for seat_id in player_agents:
            engine.get_player_view(seat_id)

    def stop(self) -> None:
        self._stop_requested = True

    async def run(self, max_hands: int | None = None, close_agents: bool = True) -> None:
        hands_played = 0
        try:
            while not self._stop_requested:
                logger.debug("Starting next hand hands_played=%s max_hands=%s", hands_played, max_hands)
                start_result = self.engine.start_next_hand()
                self._append_events(start_result.events)
                logger.debug("Engine start_next_hand result=%s events=%s", start_result, start_result.events)
                await self._deliver_updates()
                if not start_result.ok:
                    logger.debug("Stopping orchestrator because start_next_hand returned ok=False")
                    break

                await self._run_current_hand()
                hands_played += 1
                if max_hands is not None and hands_played >= max_hands:
                    break
        finally:
            await self._deliver_updates(force_table_completed=True)
            if close_agents:
                await self.close()

    async def close(self) -> None:
        for agent in self.player_agents.values():
            await agent.close()

    async def _run_current_hand(self) -> None:
        while not self.engine.is_hand_complete():
            acting_seat = self.engine.get_acting_seat()
            if acting_seat is None:
                if self.engine.get_phase() == GamePhase.HAND_COMPLETE:
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
            action = await agent.request_action(decision)
            logger.debug("Received action acting_seat=%s action=%s", acting_seat, action)
            self.last_seen_event_index[acting_seat] = len(self.event_log)
            self._pending[acting_seat].validation_error = None

            result = self.engine.apply_action(acting_seat, action)
            logger.debug("Engine apply_action result=%s events=%s", result, result.events)
            if not result.ok:
                if result.events or result.state_changed or self.engine.get_phase() == GamePhase.TABLE_COMPLETE:
                    self._append_events(result.events)
                    await self._deliver_updates()
                    logger.debug("Stopping current hand because result ended table/hand result=%s", result)
                    return
                self._pending[acting_seat].validation_error = result.error
                logger.debug("Retrying same seat due to validation error=%s", result.error)
                continue
            self._append_events(result.events)
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
