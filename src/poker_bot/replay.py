from __future__ import annotations

from dataclasses import dataclass, field

from poker_bot.poker.engine import PokerEngine
from poker_bot.types import (
    ActionType,
    GameEvent,
    HandRecord,
    HandReplayRecord,
    PlayerAction,
    ReplayAction,
    ReplayFrame,
)


class HandReplayBuildError(RuntimeError):
    pass


def build_hand_replay_record(record: HandRecord) -> HandReplayRecord:
    if record.replay_seed is None or record.replay_deck_order is None:
        raise HandReplayBuildError(f"Hand #{record.hand_number} is missing replay seed data")

    bootstrap_events, actions = _extract_replay_steps(record)
    replay_record = HandReplayRecord(
        hand_number=record.hand_number,
        seed=record.replay_seed,
        deck_order=record.replay_deck_order,
        bootstrap_events=bootstrap_events,
        actions=actions,
        ended_in_showdown=record.ended_in_showdown,
        total_steps=len(actions) + 1,
    )
    _verify_replay_record(replay_record, record)
    return replay_record


@dataclass(slots=True)
class HandReplaySession:
    record: HandReplayRecord
    viewer_seat_id: str | None = None
    current_step_index: int = 0
    _frame_cache: dict[tuple[int, str | None], ReplayFrame] = field(default_factory=dict)

    def materialize(self, step_index: int, viewer_seat_id: str | None = None) -> ReplayFrame:
        if not 0 <= step_index < self.record.total_steps:
            raise IndexError(f"Replay step {step_index} is out of range")
        viewer = viewer_seat_id if viewer_seat_id is not None else self.viewer_seat_id
        cache_key = (step_index, viewer)
        cached = self._frame_cache.get(cache_key)
        if cached is not None:
            self.current_step_index = step_index
            return cached

        engine = PokerEngine.from_hand_replay_seed(self.record.seed, self.record.deck_order)
        visible_events = list(self.record.bootstrap_events)
        focused_events = tuple(self.record.bootstrap_events)

        for replay_action in self.record.actions[:step_index]:
            result = engine.apply_action(replay_action.seat_id, replay_action.action)
            if not result.ok:
                raise HandReplayBuildError(
                    f"Replay diverged on hand #{self.record.hand_number} at step {step_index}"
                )
            visible_events.extend(result.events)
            focused_events = result.events

        frame = ReplayFrame(
            step_index=step_index,
            total_steps=self.record.total_steps,
            public_table_view=engine.get_public_table_view(),
            player_view=engine.get_player_view(viewer) if viewer is not None else None,
            visible_events=tuple(visible_events),
            focused_events=focused_events,
            revealed_seats=tuple(_revealed_seats(visible_events).items()),
            winner_amounts=tuple(_winner_amounts(visible_events).items()),
        )
        self._frame_cache[cache_key] = frame
        self.current_step_index = step_index
        return frame

    def current_frame(self) -> ReplayFrame:
        return self.materialize(self.current_step_index)

    def step_forward(self) -> ReplayFrame:
        next_step = min(self.record.total_steps - 1, self.current_step_index + 1)
        return self.materialize(next_step)

    def step_back(self) -> ReplayFrame:
        previous_step = max(0, self.current_step_index - 1)
        return self.materialize(previous_step)


def _extract_replay_steps(record: HandRecord) -> tuple[tuple[GameEvent, ...], tuple[ReplayAction, ...]]:
    bootstrap_events: list[GameEvent] = []
    replay_actions: list[ReplayAction] = []
    action_seen = False
    for event in record.events:
        if event.event_type == "action_applied":
            action_seen = True
            replay_actions.append(_replay_action_from_event(event))
            continue
        if not action_seen:
            bootstrap_events.append(event)
    return tuple(bootstrap_events), tuple(replay_actions)


def _replay_action_from_event(event: GameEvent) -> ReplayAction:
    action_type = ActionType(event.payload["action"])
    amount = event.payload.get("amount")
    if action_type in {ActionType.FOLD, ActionType.CHECK, ActionType.CALL}:
        amount = None
    return ReplayAction(
        seat_id=event.payload["seat_id"],
        action=PlayerAction(action_type=action_type, amount=amount),
    )


def _verify_replay_record(replay_record: HandReplayRecord, record: HandRecord) -> None:
    engine = PokerEngine.from_hand_replay_seed(replay_record.seed, replay_record.deck_order)
    replayed_events = list(replay_record.bootstrap_events)
    for replay_action in replay_record.actions:
        result = engine.apply_action(replay_action.seat_id, replay_action.action)
        if not result.ok:
            raise HandReplayBuildError(
                f"Replay validation failed for hand #{record.hand_number}: "
                f"{result.error.message if result.error is not None else 'unknown error'}"
            )
        replayed_events.extend(result.events)
    if tuple(replayed_events) != record.events:
        raise HandReplayBuildError(f"Replay event mismatch for hand #{record.hand_number}")
    if engine.get_public_table_view() != record.current_public_view:
        raise HandReplayBuildError(f"Replay final state mismatch for hand #{record.hand_number}")


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
