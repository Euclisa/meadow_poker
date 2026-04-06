from __future__ import annotations

from dataclasses import asdict, is_dataclass
from html import escape
import time
from typing import Any

from meadow.backend.models import BackendTableRuntime, SeatReservation, ShowdownState
from meadow.replay import replay_next_transition
from meadow.types import (
    ActionType,
    ActionValidationError,
    DecisionRequest,
    GameEvent,
    GamePhase,
    HandArchive,
    HandTransition,
    PlayerAction,
    PlayerView,
    PublicTableView,
    ReplayFrame,
    SeatSnapshot,
)


def game_event_to_dict(event: GameEvent) -> dict[str, Any]:
    return {
        "event_type": event.event_type,
        "payload": dict(event.payload),
    }


def game_event_from_dict(payload: dict[str, Any]) -> GameEvent:
    return GameEvent(
        event_type=str(payload["event_type"]),
        payload=dict(payload.get("payload", {})),
    )


def player_action_to_dict(action: PlayerAction) -> dict[str, Any]:
    return {
        "action_type": action.action_type.value,
        "amount": action.amount,
    }


def player_action_from_dict(payload: dict[str, Any]) -> PlayerAction:
    return PlayerAction(
        action_type=ActionType(str(payload["action_type"])),
        amount=payload.get("amount"),
    )


def actor_to_dict(actor: Any) -> dict[str, Any]:
    return {
        "transport": actor.transport,
        "external_id": actor.external_id,
        "display_name": actor.display_name,
        "metadata": dict(actor.metadata),
    }


def managed_table_config_to_dict(config: Any) -> dict[str, Any]:
    return asdict(config)


def serialize_waiting_tables(
    runtimes: tuple[BackendTableRuntime, ...],
    *,
    version: int | None = None,
) -> dict[str, Any]:
    resolved_version = max((runtime.version for runtime in runtimes), default=1) if version is None else version
    return {
        "version": resolved_version,
        "tables": [serialize_waiting_table(runtime) for runtime in runtimes],
    }


def serialize_waiting_table(runtime: BackendTableRuntime) -> dict[str, Any]:
    payload = {
        "table_id": runtime.table_id,
        "status": runtime.status.value,
        "share_path": f"/table/{runtime.table_id}",
        "status_message": runtime.status_message,
        "total_seats": runtime.total_seats,
        "human_transport": runtime.human_transport,
        "human_seats": runtime.human_seat_count,
        "claimed_human_seats": runtime.human_player_count,
        "llm_seats": runtime.llm_seat_count,
        "small_blind": runtime.config.small_blind,
        "big_blind": runtime.config.big_blind,
        "ante": runtime.config.ante,
        "starting_stack": runtime.config.starting_stack,
        "stack_depth": runtime.config.stack_depth,
        "turn_timeout_seconds": runtime.config.turn_timeout_seconds,
        "idle_close_seconds": runtime.config.idle_close_seconds,
        "waiting_players": [
            {
                "display_name": reservation.actor.display_name,
                "seat_id": reservation.seat_id,
                "is_creator": runtime.is_creator_token(reservation.viewer_token),
            }
            for reservation in runtime.reservations
            if reservation.is_seated and reservation.seat_id is not None
        ],
    }
    if runtime.human_transport == "web":
        payload["web_seats"] = runtime.human_seat_count
        payload["claimed_web_seats"] = runtime.human_player_count
    if runtime.human_transport == "telegram":
        payload["telegram_seats_total"] = runtime.human_seat_count
        payload["telegram_seats_claimed"] = runtime.human_player_count
    return payload


def serialize_table_snapshot(
    runtime: BackendTableRuntime,
    *,
    viewer_token: str | None,
) -> dict[str, Any]:
    viewer = runtime.find_reservation_by_token(viewer_token)
    viewer_seat_id = viewer.seat_id if viewer is not None and viewer.seat_id is not None else None

    player_view = None
    public_table = None
    pending_decision = None
    if runtime.engine is not None:
        public_view = runtime.engine.get_public_table_view()
        public_table = _serialize_public_table(public_view, viewer_seat_id=viewer_seat_id)
        if viewer_seat_id is not None:
            player_view = _serialize_player_view(runtime.engine.get_player_view(viewer_seat_id))
            agent = runtime.human_agents.get(viewer_seat_id)
            if agent is not None and agent.pending_decision is not None:
                pending_decision = _serialize_decision(agent.pending_decision)
    else:
        public_view = None

    payload = {
        "version": runtime.version,
        "status": runtime.status.value,
        "table_id": runtime.table_id,
        "replay": None,
        "viewer_actor": actor_to_dict(viewer.actor) if viewer is not None else None,
        "config_summary": {
            "total_seats": runtime.total_seats,
            "human_transport": runtime.human_transport,
            "human_seats": runtime.human_seat_count,
            "claimed_human_seats": runtime.human_player_count,
            "llm_seats": runtime.llm_seat_count,
            "small_blind": runtime.config.small_blind,
            "big_blind": runtime.config.big_blind,
            "ante": runtime.config.ante,
            "starting_stack": runtime.config.starting_stack,
            "stack_depth": runtime.config.stack_depth,
            "turn_timeout_seconds": runtime.config.turn_timeout_seconds,
            "idle_close_seconds": runtime.config.idle_close_seconds,
            "max_players": runtime.config.max_players,
            "max_hands_per_table": runtime.config.max_hands_per_table,
            "share_path": f"/table/{runtime.table_id}",
        },
        "waiting_players": [
            {
                "display_name": reservation.actor.display_name,
                "seat_id": reservation.seat_id,
                "is_creator": runtime.is_creator_token(reservation.viewer_token),
            }
            for reservation in runtime.reservations
            if reservation.is_seated and reservation.seat_id is not None
        ],
        "public_table": public_table,
        "player_view": player_view,
        "pending_decision": pending_decision,
        "turn_timer": _serialize_turn_timer(runtime),
        "seat_amount_badges": _serialize_seat_amount_badges(public_view, runtime.showdown_state),
        "recent_events": _serialize_recent_events(runtime),
        "completed_hands": _serialize_completed_hands(runtime),
        "controls": _serialize_controls(runtime, viewer=viewer, has_pending_decision=pending_decision is not None),
        "message": runtime.status_message,
        "showdown": _serialize_showdown(runtime.showdown_state),
    }
    if runtime.human_transport == "web":
        payload["config_summary"]["web_seats"] = runtime.human_seat_count
        payload["config_summary"]["claimed_web_seats"] = runtime.human_player_count
    if runtime.human_transport == "telegram":
        payload["config_summary"]["telegram_seats_total"] = runtime.human_seat_count
        payload["config_summary"]["telegram_seats_claimed"] = runtime.human_player_count
    if viewer is not None:
        payload["participants"] = serialize_private_participants(runtime)
    return payload


def serialize_replay_snapshot(
    runtime: BackendTableRuntime,
    archive: HandArchive,
    frame: ReplayFrame,
    *,
    viewer_token: str | None,
) -> dict[str, Any]:
    viewer = runtime.find_reservation_by_token(viewer_token)
    viewer_seat_id = viewer.seat_id if viewer is not None and viewer.seat_id is not None else None
    analysis = _serialize_replay_analysis(archive, frame, viewer_seat_id=viewer_seat_id)
    public_table = _serialize_public_table(
        frame.public_table_view,
        viewer_seat_id=viewer_seat_id,
    )
    payload = {
        "version": runtime.version,
        "status": runtime.status.value,
        "table_id": runtime.table_id,
        "viewer_actor": actor_to_dict(viewer.actor) if viewer is not None else None,
        "replay": {
            "active": True,
            "hand_number": archive.record.hand_number,
            "current_step": frame.step_index,
            "total_steps": frame.total_steps,
            "can_step_backward": frame.step_index > 0,
            "can_step_forward": frame.step_index < frame.total_steps - 1,
            "replay_path": f"/table/{runtime.table_id}/replay/{archive.record.hand_number}",
            "analysis": analysis,
        },
        "config_summary": {
            "total_seats": runtime.total_seats,
            "human_transport": runtime.human_transport,
            "human_seats": runtime.human_seat_count,
            "claimed_human_seats": runtime.human_player_count,
            "llm_seats": runtime.llm_seat_count,
            "small_blind": runtime.config.small_blind,
            "big_blind": runtime.config.big_blind,
            "ante": runtime.config.ante,
            "starting_stack": runtime.config.starting_stack,
            "stack_depth": runtime.config.stack_depth,
            "turn_timeout_seconds": runtime.config.turn_timeout_seconds,
            "idle_close_seconds": runtime.config.idle_close_seconds,
            "max_players": runtime.config.max_players,
            "max_hands_per_table": runtime.config.max_hands_per_table,
            "share_path": f"/table/{runtime.table_id}",
        },
        "waiting_players": [],
        "public_table": public_table,
        "player_view": _serialize_player_view(frame.player_view) if frame.player_view is not None else None,
        "pending_decision": None,
        "turn_timer": _empty_turn_timer(),
        "seat_amount_badges": _serialize_replay_seat_amount_badges(frame),
        "recent_events": _serialize_replay_events(frame),
        "completed_hands": _serialize_completed_hands(runtime),
        "controls": {
            "seat_token_valid": viewer is not None,
            "viewer_name": viewer.actor.display_name if viewer is not None else None,
            "is_joined": viewer is not None,
            "is_creator": viewer is not None and runtime.is_creator_token(viewer.viewer_token),
            "can_join": False,
            "can_start": False,
            "can_leave": False,
            "can_cancel": False,
            "can_act": False,
            "can_request_coach": analysis["eligible"] and runtime.coach is not None,
            "share_path": f"/table/{runtime.table_id}",
            "join_disabled_reason": None,
        },
        "message": f"Replay for hand #{archive.record.hand_number}",
        "showdown": _serialize_replay_showdown(frame),
    }
    if runtime.human_transport == "web":
        payload["config_summary"]["web_seats"] = runtime.human_seat_count
        payload["config_summary"]["claimed_web_seats"] = runtime.human_player_count
    if runtime.human_transport == "telegram":
        payload["config_summary"]["telegram_seats_total"] = runtime.human_seat_count
        payload["config_summary"]["telegram_seats_claimed"] = runtime.human_player_count
    if viewer is not None:
        payload["participants"] = serialize_private_participants(runtime)
    return payload


def serialize_private_participants(runtime: BackendTableRuntime) -> list[dict[str, Any]]:
    return [
        {
            "seat_id": reservation.seat_id,
            "display_name": reservation.actor.display_name,
            "transport": reservation.actor.transport,
            "external_id": reservation.actor.external_id,
            "metadata": dict(reservation.actor.metadata),
            "is_creator": runtime.is_creator_token(reservation.viewer_token),
            "is_seated": reservation.is_seated,
        }
        for reservation in runtime.reservations
    ]


def snapshot_player_view(payload: dict[str, Any], public_table_payload: dict[str, Any]) -> PlayerView:
    public_table = snapshot_public_table_view(public_table_payload)
    return PlayerView(
        seat_id=str(payload["seat_id"]),
        player_name=str(payload["player_name"]),
        hole_cards=tuple(payload.get("hole_cards", ())),
        stack=int(payload["stack"]),
        contribution=int(payload["contribution"]),
        position=payload.get("position"),
        to_call=int(payload["to_call"]),
        public_table=public_table,
    )


def snapshot_public_table_view(payload: dict[str, Any]) -> PublicTableView:
    return PublicTableView(
        hand_number=int(payload["hand_number"]),
        phase=GamePhase(str(payload["phase"])),
        board_cards=tuple(payload.get("board_cards", ())),
        pot_total=int(payload["pot_total"]),
        current_bet=int(payload["current_bet"]),
        dealer_seat_id=payload.get("dealer_seat_id"),
        acting_seat_id=payload.get("acting_seat_id"),
        small_blind=int(payload["small_blind"]),
        big_blind=int(payload["big_blind"]),
        ante=int(payload.get("ante", 0)),
        seats=tuple(
            SeatSnapshot(
                seat_id=str(item["seat_id"]),
                name=str(item["name"]),
                stack=int(item["stack"]),
                contribution=int(item["contribution"]),
                folded=bool(item["folded"]),
                all_in=bool(item["all_in"]),
                in_hand=bool(item["in_hand"]),
                position=item.get("position"),
                street_contribution=int(item.get("street_contribution", 0)),
            )
            for item in payload["seats"]
        ),
    )


def snapshot_pending_decision(payload: dict[str, Any], snapshot: dict[str, Any]) -> DecisionRequest:
    public_table_payload = snapshot["public_table"]
    player_view_payload = snapshot["player_view"]
    assert public_table_payload is not None
    assert player_view_payload is not None
    return DecisionRequest(
        acting_seat_id=str(payload["acting_seat_id"]),
        player_view=snapshot_player_view(player_view_payload, public_table_payload),
        public_table_view=snapshot_public_table_view(public_table_payload),
        legal_actions=tuple(
            _legal_action_from_payload(item)
            for item in payload.get("legal_actions", ())
        ),
        validation_error=(
            ActionValidationError(
                code=str(payload["validation_error"]["code"]),
                message=str(payload["validation_error"]["message"]),
            )
            if payload.get("validation_error") is not None
            else None
        ),
        turn_timeout_seconds=payload.get("turn_timeout_seconds"),
    )


def jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {key: jsonable(item) for key, item in asdict(value).items()}
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [jsonable(item) for item in value]
    if isinstance(value, list):
        return [jsonable(item) for item in value]
    if hasattr(value, "value"):
        return value.value
    return value


def _legal_action_from_payload(payload: dict[str, Any]) -> Any:
    from meadow.types import LegalAction

    return LegalAction(
        action_type=ActionType(str(payload["action_type"])),
        min_amount=payload.get("min_amount"),
        max_amount=payload.get("max_amount"),
    )


def _serialize_replay_analysis(
    archive: HandArchive,
    frame: ReplayFrame,
    *,
    viewer_seat_id: str | None,
) -> dict[str, Any]:
    transition = replay_next_transition(archive.trace, frame.step_index)
    seat_names = {seat.seat_id: seat.name for seat in frame.public_table_view.seats}
    if transition is None:
        return {
            "eligible": False,
            "status": "complete",
            "message": "No decision spot here",
            "next_action": None,
        }
    if transition.kind != "action":
        return {
            "eligible": False,
            "status": "automatic",
            "message": "No decision spot here",
            "next_action": None,
        }
    next_action = _serialize_replay_action(transition, seat_names=seat_names)
    if viewer_seat_id is None:
        return {
            "eligible": False,
            "status": "viewer_required",
            "message": next_action["label"],
            "next_action": next_action,
        }
    if transition.seat_id != viewer_seat_id:
        return {
            "eligible": False,
            "status": "other_player_action",
            "message": f"Next: {next_action['actor_name']} acts",
            "next_action": next_action,
        }
    return {
        "eligible": True,
        "status": "viewer_action",
        "message": next_action["label"],
        "next_action": next_action,
    }


def _serialize_controls(
    runtime: BackendTableRuntime,
    *,
    viewer: SeatReservation | None,
    has_pending_decision: bool,
) -> dict[str, Any]:
    token_valid = viewer is not None
    is_creator = viewer is not None and runtime.is_creator_token(viewer.viewer_token)
    viewer_is_seated = viewer is not None and viewer.is_seated
    can_join = runtime.status.value == "waiting" and viewer is None and not runtime.is_full()
    return {
        "seat_token_valid": token_valid,
        "viewer_name": viewer.actor.display_name if viewer is not None else None,
        "is_joined": viewer_is_seated,
        "is_creator": is_creator,
        "can_join": can_join,
        "can_start": token_valid and is_creator and runtime.status.value == "waiting" and (runtime.is_full() or runtime.human_seat_count == 0),
        "can_leave": viewer_is_seated and not is_creator and runtime.status.value == "waiting",
        "can_cancel": token_valid and is_creator and runtime.status.value == "waiting",
        "can_act": has_pending_decision,
        "can_request_coach": has_pending_decision and runtime.coach is not None,
        "share_path": f"/table/{runtime.table_id}",
        "join_disabled_reason": None if can_join else _join_disabled_reason(runtime, viewer),
    }


def _join_disabled_reason(runtime: BackendTableRuntime, viewer: SeatReservation | None) -> str | None:
    if viewer is not None:
        if viewer.is_seated:
            return "You already have a seat at this table."
        return "You are already connected to this table."
    if runtime.status.value != "waiting":
        return "This table is no longer accepting new players."
    if runtime.is_full():
        return f"All {runtime.human_transport} seats are already claimed."
    return None


def _serialize_recent_events(runtime: BackendTableRuntime) -> list[dict[str, Any]]:
    seat_names = None
    if runtime.engine is not None:
        seat_names = {
            seat.seat_id: seat.name
            for seat in runtime.engine.get_public_table_view().seats
        }
    game_events = []
    orchestrator = runtime.orchestrator
    if orchestrator is not None:
        for index, event in enumerate(orchestrator.event_log, start=1):
            text = _render_event_html(event, seat_names=seat_names)
            if not text:
                continue
            game_events.append(
                {
                    "id": f"game-{index}",
                    "kind": _event_kind(event),
                    "event_type": event.event_type,
                    "text": text,
                }
            )
    activity = [{**entry, "text": escape(entry["text"])} for entry in runtime.activity_log]
    return [*activity, *game_events][-40:]


def _serialize_replay_events(frame: ReplayFrame) -> list[dict[str, Any]]:
    seat_names = {seat.seat_id: seat.name for seat in frame.public_table_view.seats}
    result = []
    for index, event in enumerate(frame.visible_events, start=1):
        text = _render_event_html(event, seat_names=seat_names)
        if not text:
            continue
        result.append(
            {
                "id": f"replay-{index}",
                "kind": _event_kind(event),
                "event_type": event.event_type,
                "text": text,
            }
        )
    return result[-40:]


def _serialize_completed_hands(runtime: BackendTableRuntime) -> list[dict[str, Any]]:
    orchestrator = runtime.orchestrator
    if orchestrator is None:
        return []
    return [
        {
            "hand_number": archive.record.hand_number,
            "ended_in_showdown": archive.record.ended_in_showdown,
            "replay_path": f"/table/{runtime.table_id}/replay/{archive.record.hand_number}",
        }
        for archive in reversed(orchestrator.completed_hand_archives)
    ]


def _serialize_showdown(showdown: ShowdownState | None) -> dict[str, Any] | None:
    if showdown is None:
        return None
    return {
        "active": True,
        "revealed_seats": [
            {
                "seat_id": seat.seat_id,
                "hole_cards": list(seat.hole_cards),
            }
            for seat in showdown.revealed_seats
        ],
    }


def _serialize_replay_showdown(frame: ReplayFrame) -> dict[str, Any] | None:
    if not frame.revealed_seats:
        return None
    return {
        "active": True,
        "revealed_seats": [
            {
                "seat_id": seat_id,
                "hole_cards": list(hole_cards),
            }
            for seat_id, hole_cards in frame.revealed_seats
        ],
    }


def _serialize_seat_amount_badges(
    public_view: PublicTableView | None,
    showdown: ShowdownState | None,
) -> list[dict[str, Any]]:
    if showdown is not None:
        return [
            {"seat_id": winner.seat_id, "amount": winner.amount}
            for winner in showdown.winners
            if winner.amount > 0
        ]
    if public_view is None:
        return []
    if public_view.phase not in {
        GamePhase.PREFLOP,
        GamePhase.FLOP,
        GamePhase.TURN,
        GamePhase.RIVER,
        GamePhase.SHOWDOWN,
    }:
        return []
    return [
        {"seat_id": seat.seat_id, "amount": seat.street_contribution}
        for seat in public_view.seats
        if seat.street_contribution > 0
    ]


def _serialize_replay_seat_amount_badges(frame: ReplayFrame) -> list[dict[str, Any]]:
    if frame.winner_amounts:
        return [
            {"seat_id": seat_id, "amount": amount}
            for seat_id, amount in frame.winner_amounts
            if amount > 0
        ]
    if frame.public_table_view.phase not in {
        GamePhase.PREFLOP,
        GamePhase.FLOP,
        GamePhase.TURN,
        GamePhase.RIVER,
        GamePhase.SHOWDOWN,
    }:
        return []
    return [
        {"seat_id": seat.seat_id, "amount": seat.street_contribution}
        for seat in frame.public_table_view.seats
        if seat.street_contribution > 0
    ]


def _serialize_replay_action(
    transition: HandTransition,
    *,
    seat_names: dict[str, str],
) -> dict[str, Any]:
    action = transition.action
    seat_id = transition.seat_id or "unknown"
    actor_name = seat_names.get(seat_id, seat_id)
    action_type = action.action_type.value if action is not None else "unknown"
    amount = action.amount if action is not None else None
    if amount is not None:
        label = f"{actor_name} {action_type} {amount}"
    else:
        label = f"{actor_name} {action_type}"
    return {
        "seat_id": seat_id,
        "actor_name": actor_name,
        "action_type": action_type,
        "amount": amount,
        "label": label,
    }


def _render_event_html(event: GameEvent, *, seat_names: dict[str, str] | None = None) -> str | None:
    payload = event.payload
    seat_id = payload.get("seat_id")
    raw_name = seat_id if seat_names is None or seat_id is None else seat_names.get(seat_id, seat_id)
    name = f"<b>{escape(raw_name)}</b>" if raw_name else "unknown"
    if event.event_type == "action_applied":
        amount = payload.get("amount")
        action = escape(payload["action"])
        if amount is None:
            return f"{name} {action}"
        return f"{name} {action} {amount}"
    if event.event_type == "blind_posted":
        return f"{name} {escape(payload['blind'])} blind {payload['amount']}"
    if event.event_type == "ante_posted":
        return f"{name} ante {payload['amount']}"
    if event.event_type == "street_started":
        phase = escape(payload["phase"].replace("_", " ").title())
        board_cards = payload.get("board_cards", ())
        board = " ".join(board_cards) if board_cards else None
        if board:
            return f"<b>{phase}</b>: {escape(board)}"
        return f"<b>{phase}</b>"
    if event.event_type == "pot_awarded":
        return f"{name} won {payload['amount']}"
    if event.event_type == "hand_awarded":
        return f"{name} collected {payload['amount']}"
    if event.event_type == "hand_started":
        return f"Hand <b>{payload['hand_number']}</b>"
    if event.event_type == "hand_completed":
        return f"Hand <b>{payload['hand_number']}</b> complete"
    if event.event_type == "showdown_started":
        board_cards = payload.get("board_cards", ())
        board = " ".join(board_cards) if board_cards else "-"
        return f"<b>Showdown</b>: {escape(board)}"
    if event.event_type == "showdown_revealed":
        cards = escape(" ".join(payload.get("hole_cards", ())) or "-")
        hand_label = escape(payload["hand_label"])
        return f"{name} showed {cards} <i>({hand_label})</i>"
    if event.event_type == "table_completed":
        reason = escape(payload.get("reason", "unknown").replace("_", " "))
        return f"Table finished <i>({reason})</i>"
    if event.event_type == "chips_refunded":
        return f"{name} refunded {payload['amount']}"
    return None


def _event_kind(event: GameEvent) -> str:
    if event.event_type in {"pot_awarded", "hand_awarded", "chips_refunded"}:
        return "reward"
    if event.event_type in {"action_applied", "ante_posted", "blind_posted", "showdown_revealed"}:
        return "action"
    return "state"


def _serialize_public_table(
    view: PublicTableView,
    *,
    viewer_seat_id: str | None,
) -> dict[str, Any]:
    return {
        "hand_number": view.hand_number,
        "phase": view.phase.value,
        "board_cards": list(view.board_cards),
        "pot_total": view.pot_total,
        "current_bet": view.current_bet,
        "dealer_seat_id": view.dealer_seat_id,
        "acting_seat_id": view.acting_seat_id,
        "small_blind": view.small_blind,
        "big_blind": view.big_blind,
        "ante": view.ante,
        "seats": [
            _serialize_seat(seat, is_viewer=seat.seat_id == viewer_seat_id)
            for seat in view.seats
        ],
    }


def _serialize_seat(seat: SeatSnapshot, *, is_viewer: bool) -> dict[str, Any]:
    return {
        "seat_id": seat.seat_id,
        "name": seat.name,
        "stack": seat.stack,
        "contribution": seat.contribution,
        "street_contribution": seat.street_contribution,
        "folded": seat.folded,
        "all_in": seat.all_in,
        "in_hand": seat.in_hand,
        "position": seat.position,
        "is_viewer": is_viewer,
    }


def _serialize_player_view(view: PlayerView) -> dict[str, Any]:
    return {
        "seat_id": view.seat_id,
        "player_name": view.player_name,
        "hole_cards": list(view.hole_cards),
        "stack": view.stack,
        "contribution": view.contribution,
        "position": view.position,
        "to_call": view.to_call,
    }


def _serialize_decision(decision: DecisionRequest) -> dict[str, Any]:
    return {
        "acting_seat_id": decision.acting_seat_id,
        "to_call": decision.player_view.to_call,
        "turn_timeout_seconds": decision.turn_timeout_seconds,
        "validation_error": (
            {
                "code": decision.validation_error.code,
                "message": decision.validation_error.message,
            }
            if decision.validation_error is not None
            else None
        ),
        "legal_actions": [
            {
                "action_type": item.action_type.value,
                "min_amount": item.min_amount,
                "max_amount": item.max_amount,
            }
            for item in decision.legal_actions
        ],
    }


def _serialize_turn_timer(runtime: BackendTableRuntime) -> dict[str, Any]:
    orchestrator = runtime.orchestrator
    if orchestrator is None or orchestrator.current_turn_timer is None:
        return _empty_turn_timer()
    timer = orchestrator.current_turn_timer
    return {
        "enabled": True,
        "seat_id": timer.seat_id,
        "duration_ms": timer.duration_seconds * 1000,
        "deadline_epoch_ms": timer.deadline_epoch_ms,
        "server_now_epoch_ms": int(time.time() * 1000),
    }


def _empty_turn_timer() -> dict[str, Any]:
    return {
        "enabled": False,
        "seat_id": None,
        "duration_ms": None,
        "deadline_epoch_ms": None,
        "server_now_epoch_ms": int(time.time() * 1000),
    }
