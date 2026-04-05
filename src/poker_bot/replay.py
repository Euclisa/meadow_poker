from __future__ import annotations

from dataclasses import dataclass, field

from poker_bot.poker.engine import PokerEngine
from poker_bot.types import DecisionRequest, GameEvent, HandTransition, HandTrace, ReplayFrame


class HandReplayBuildError(RuntimeError):
    pass


class ReplayAnalysisError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ReplayDecisionSpot:
    frame: ReplayFrame
    decision: DecisionRequest
    next_transition: HandTransition


@dataclass(slots=True)
class HandReplaySession:
    trace: HandTrace
    viewer_seat_id: str | None = None
    current_step_index: int = 0
    _frame_cache: dict[tuple[int, str | None], ReplayFrame] = field(default_factory=dict)

    def materialize(self, step_index: int, viewer_seat_id: str | None = None) -> ReplayFrame:
        if not 0 <= step_index < self.trace.total_steps:
            raise IndexError(f"Replay step {step_index} is out of range")
        viewer = viewer_seat_id if viewer_seat_id is not None else self.viewer_seat_id
        cache_key = (step_index, viewer)
        cached = self._frame_cache.get(cache_key)
        if cached is not None:
            self.current_step_index = step_index
            return cached

        _engine, frame = _materialize_replay_state(self.trace, step_index, viewer)
        self._frame_cache[cache_key] = frame
        self.current_step_index = step_index
        return frame

    def current_frame(self) -> ReplayFrame:
        return self.materialize(self.current_step_index)

    def step_forward(self) -> ReplayFrame:
        next_step = min(self.trace.total_steps - 1, self.current_step_index + 1)
        return self.materialize(next_step)

    def step_back(self) -> ReplayFrame:
        previous_step = max(0, self.current_step_index - 1)
        return self.materialize(previous_step)


def validate_hand_trace(trace: HandTrace) -> None:
    engine = PokerEngine.from_hand_state_snapshot(trace.initial_state)
    replayed_events = list(trace.initial_events)
    for index, transition in enumerate(trace.transitions, start=1):
        emitted_events = _apply_transition(engine, transition)
        if emitted_events != transition.events:
            raise HandReplayBuildError(
                f"Replay validation failed for hand #{trace.hand_number} at transition {index}"
            )
        replayed_events.extend(emitted_events)
    if trace.final_state is not None:
        if engine.export_hand_state_snapshot() != trace.final_state:
            raise HandReplayBuildError(f"Replay final state mismatch for hand #{trace.hand_number}")


def build_replay_decision_spot(
    trace: HandTrace,
    *,
    step_index: int,
    viewer_seat_id: str,
) -> ReplayDecisionSpot:
    if not viewer_seat_id:
        raise ReplayAnalysisError("Replay coach requires a viewer seat.")
    engine, frame = _materialize_replay_state(trace, step_index, viewer_seat_id)
    next_transition = replay_next_transition(trace, step_index)
    if next_transition is None:
        raise ReplayAnalysisError("Replay coach is not available on the final replay step.")
    if next_transition.kind != "action":
        raise ReplayAnalysisError("Replay coach is only available before a recorded player action.")
    if next_transition.seat_id != viewer_seat_id:
        raise ReplayAnalysisError("Replay coach is only available for your own recorded action spots.")
    decision = engine.get_decision_request(viewer_seat_id)
    if engine.get_acting_seat() != viewer_seat_id or not decision.legal_actions:
        raise ReplayAnalysisError("Replay decision context is unavailable for this spot.")
    return ReplayDecisionSpot(frame=frame, decision=decision, next_transition=next_transition)


def replay_next_transition(trace: HandTrace, step_index: int) -> HandTransition | None:
    if not 0 <= step_index < trace.total_steps:
        raise IndexError(f"Replay step {step_index} is out of range")
    if step_index >= len(trace.transitions):
        return None
    return trace.transitions[step_index]


def _apply_transition(engine: PokerEngine, transition: HandTransition) -> tuple[GameEvent, ...]:
    if transition.kind == "action":
        if transition.seat_id is None or transition.action is None:
            raise HandReplayBuildError("Action transitions must include both seat_id and action")
        result = engine.apply_action(
            transition.seat_id,
            transition.action,
            auto_resolve=False,
        )
        if not result.ok:
            raise HandReplayBuildError(
                f"Replay action failed for seat {transition.seat_id}: "
                f"{result.error.message if result.error is not None else 'unknown error'}"
            )
        return result.events
    if transition.kind == "automatic":
        progress = engine.resolve_automatic_step()
        if not progress.advanced:
            raise HandReplayBuildError("Replay expected an automatic transition but none was pending")
        return progress.events
    raise HandReplayBuildError(f"Unknown replay transition kind: {transition.kind}")


def _materialize_replay_state(
    trace: HandTrace,
    step_index: int,
    viewer_seat_id: str | None,
) -> tuple[PokerEngine, ReplayFrame]:
    if not 0 <= step_index < trace.total_steps:
        raise IndexError(f"Replay step {step_index} is out of range")

    engine = PokerEngine.from_hand_state_snapshot(trace.initial_state)
    visible_events = list(trace.initial_events)
    focused_events = trace.initial_events

    for index, transition in enumerate(trace.transitions[:step_index], start=1):
        emitted_events = _apply_transition(engine, transition)
        if emitted_events != transition.events:
            raise HandReplayBuildError(
                f"Replay diverged on hand #{trace.hand_number} at transition {index}"
            )
        visible_events.extend(emitted_events)
        focused_events = emitted_events

    frame = ReplayFrame(
        step_index=step_index,
        total_steps=trace.total_steps,
        public_table_view=engine.get_public_table_view(),
        player_view=engine.get_player_view(viewer_seat_id) if viewer_seat_id is not None else None,
        visible_events=tuple(visible_events),
        focused_events=focused_events,
        revealed_seats=tuple(_revealed_seats(visible_events).items()),
        winner_amounts=tuple(_winner_amounts(visible_events).items()),
    )
    return engine, frame


def _revealed_seats(events: list[GameEvent]) -> dict[str, tuple[str, str]]:
    revealed: dict[str, tuple[str, str]] = {}
    for event in events:
        if event.event_type != "showdown_revealed":
            continue
        hole_cards = tuple(event.payload["hole_cards"])
        revealed[event.payload["seat_id"]] = (hole_cards[0], hole_cards[1])
    return revealed


def _winner_amounts(events: list[GameEvent]) -> dict[str, int]:
    winners: dict[str, int] = {}
    for event in events:
        if event.event_type != "pot_awarded":
            continue
        seat_id = event.payload["seat_id"]
        winners[seat_id] = winners.get(seat_id, 0) + int(event.payload["amount"])
    return winners
