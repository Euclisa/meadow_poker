from __future__ import annotations

from typing import Any

from poker_bot.players.rendering import render_events
from poker_bot.types import (
    DecisionRequest,
    GameEvent,
    GamePhase,
    HandArchive,
    PlayerView,
    PublicTableView,
    ReplayFrame,
    SeatSnapshot,
)
from poker_bot.web_app.player import WebPlayerAgent
from poker_bot.web_app.session import WebShowdownState, WebTableSession, WebUserReservation


def serialize_lobby(registry: Any) -> dict[str, Any]:
    return {
        "tables": [serialize_waiting_table(session) for session in registry.list_waiting_tables()],
    }


def serialize_waiting_table(session: WebTableSession) -> dict[str, Any]:
    return {
        "table_id": session.table_id,
        "status": session.status.value,
        "share_path": f"/table/{session.table_id}",
        "status_message": session.status_message,
        "total_seats": session.total_seats,
        "web_seats": session.web_seat_count,
        "claimed_web_seats": session.human_player_count,
        "llm_seats": session.llm_seat_count,
        "small_blind": session.request.small_blind,
        "big_blind": session.request.big_blind,
        "starting_stack": session.request.starting_stack,
        "stack_depth": session.request.stack_depth,
        "waiting_players": [
            {
                "display_name": user.display_name,
                "seat_id": user.seat_id,
                "is_creator": session.is_creator_token(user.seat_token),
            }
            for user in session.claimed_web_users
        ],
    }


def serialize_table_snapshot(
    session: WebTableSession,
    *,
    seat_token: str | None,
    small_blind: int,
    big_blind: int,
    starting_stack: int,
    max_players: int,
    max_hands_per_table: int | None,
) -> dict[str, Any]:
    viewer = session.find_reservation_by_token(seat_token)
    human_seat_ids = {user.seat_id for user in session.claimed_web_users}
    viewer_seat_id = viewer.seat_id if viewer is not None else None

    player_view = None
    public_table = None
    pending_decision = None
    if session.engine is not None:
        public_view = session.engine.get_public_table_view()
        public_table = _serialize_public_table(public_view, human_seat_ids=human_seat_ids, viewer_seat_id=viewer_seat_id)
        if viewer is not None:
            player_view = _serialize_player_view(session.engine.get_player_view(viewer.seat_id))
            agent = session.player_agents.get(viewer.seat_id)
            if isinstance(agent, WebPlayerAgent) and agent.pending_decision is not None:
                pending_decision = _serialize_decision(agent.pending_decision)
    else:
        public_view = None

    return {
        "status": session.status.value,
        "table_id": session.table_id,
        "replay": None,
        "config_summary": {
            "total_seats": session.total_seats,
            "web_seats": session.web_seat_count,
            "claimed_web_seats": session.human_player_count,
            "llm_seats": session.llm_seat_count,
            "small_blind": small_blind,
            "big_blind": big_blind,
            "starting_stack": starting_stack,
            "stack_depth": session.request.stack_depth,
            "max_players": max_players,
            "max_hands_per_table": max_hands_per_table,
            "share_path": f"/table/{session.table_id}",
        },
        "waiting_players": [
            {
                "display_name": user.display_name,
                "seat_id": user.seat_id,
                "is_creator": session.is_creator_token(user.seat_token),
            }
            for user in session.claimed_web_users
        ],
        "public_table": public_table,
        "player_view": player_view,
        "pending_decision": pending_decision,
        "seat_amount_badges": _serialize_seat_amount_badges(public_view, session.showdown_state),
        "recent_events": _serialize_recent_events(session),
        "completed_hands": _serialize_completed_hands(session),
        "controls": _serialize_controls(session, viewer=viewer, has_pending_decision=pending_decision is not None),
        "message": session.status_message,
        "showdown": _serialize_showdown(session.showdown_state),
    }


def serialize_replay_snapshot(
    session: WebTableSession,
    archive: HandArchive,
    frame: ReplayFrame,
    *,
    seat_token: str | None,
) -> dict[str, Any]:
    viewer = session.find_reservation_by_token(seat_token)
    viewer_seat_id = viewer.seat_id if viewer is not None else None
    human_seat_ids = {user.seat_id for user in session.claimed_web_users}
    public_table = _serialize_public_table(
        frame.public_table_view,
        human_seat_ids=human_seat_ids,
        viewer_seat_id=viewer_seat_id,
    )
    return {
        "status": session.status.value,
        "table_id": session.table_id,
        "replay": {
            "active": True,
            "hand_number": archive.record.hand_number,
            "current_step": frame.step_index,
            "total_steps": frame.total_steps,
            "can_step_backward": frame.step_index > 0,
            "can_step_forward": frame.step_index < frame.total_steps - 1,
            "replay_path": f"/table/{session.table_id}/replay/{archive.record.hand_number}",
        },
        "config_summary": {
            "total_seats": session.total_seats,
            "web_seats": session.web_seat_count,
            "claimed_web_seats": session.human_player_count,
            "llm_seats": session.llm_seat_count,
            "small_blind": session.request.small_blind,
            "big_blind": session.request.big_blind,
            "starting_stack": session.request.starting_stack,
            "stack_depth": session.request.stack_depth,
            "max_players": session.total_seats,
            "max_hands_per_table": None,
            "share_path": f"/table/{session.table_id}",
        },
        "waiting_players": [],
        "public_table": public_table,
        "player_view": _serialize_player_view(frame.player_view) if frame.player_view is not None else None,
        "pending_decision": None,
        "seat_amount_badges": _serialize_replay_seat_amount_badges(frame),
        "recent_events": _serialize_replay_events(frame),
        "completed_hands": _serialize_completed_hands(session),
        "controls": {
            "seat_token_valid": viewer is not None,
            "viewer_name": viewer.display_name if viewer is not None else None,
            "is_joined": viewer is not None,
            "is_creator": viewer is not None and session.is_creator_token(viewer.seat_token),
            "can_join": False,
            "can_start": False,
            "can_leave": False,
            "can_cancel": False,
            "can_act": False,
            "can_request_coach": False,
            "share_path": f"/table/{session.table_id}",
            "join_disabled_reason": None,
        },
        "message": f"Replay for hand #{archive.record.hand_number}",
        "showdown": _serialize_replay_showdown(frame),
    }


def _serialize_controls(
    session: WebTableSession,
    *,
    viewer: WebUserReservation | None,
    has_pending_decision: bool,
) -> dict[str, Any]:
    token_valid = viewer is not None
    is_creator = viewer is not None and session.is_creator_token(viewer.seat_token)
    can_join = session.status.value == "waiting" and viewer is None and not session.is_full()
    return {
        "seat_token_valid": token_valid,
        "viewer_name": viewer.display_name if viewer is not None else None,
        "is_joined": token_valid,
        "is_creator": is_creator,
        "can_join": can_join,
        "can_start": token_valid and is_creator and session.status.value == "waiting" and session.is_full(),
        "can_leave": token_valid and not is_creator and session.status.value == "waiting",
        "can_cancel": token_valid and is_creator and session.status.value == "waiting",
        "can_act": has_pending_decision,
        "can_request_coach": has_pending_decision and session.coach is not None,
        "share_path": f"/table/{session.table_id}",
        "join_disabled_reason": None if can_join else _join_disabled_reason(session, viewer),
    }


def _join_disabled_reason(session: WebTableSession, viewer: WebUserReservation | None) -> str | None:
    if viewer is not None:
        return "You already have a seat at this table."
    if session.status.value != "waiting":
        return "This table is no longer accepting new players."
    if session.is_full():
        return "All web seats are already claimed."
    return None


def _serialize_recent_events(session: WebTableSession) -> list[dict[str, Any]]:
    seat_names = None
    if session.engine is not None:
        seat_names = {
            seat.seat_id: seat.name
            for seat in session.engine.get_public_table_view().seats
        }

    game_events = []
    if session.orchestrator is not None:
        for index, event in enumerate(session.orchestrator.event_log, start=1):
            game_events.append(
                {
                    "id": f"game-{index}",
                    "kind": _event_kind(event),
                    "event_type": event.event_type,
                    "text": render_events((event,), seat_names=seat_names),
                }
            )

    return [*session.activity_log, *game_events][-40:]


def _serialize_replay_events(frame: ReplayFrame) -> list[dict[str, Any]]:
    seat_names = {seat.seat_id: seat.name for seat in frame.public_table_view.seats}
    return [
        {
            "id": f"replay-{index}",
            "kind": _event_kind(event),
            "event_type": event.event_type,
            "text": render_events((event,), seat_names=seat_names),
        }
        for index, event in enumerate(frame.visible_events, start=1)
    ][-40:]


def _serialize_completed_hands(session: WebTableSession) -> list[dict[str, Any]]:
    if session.orchestrator is None:
        return []
    return [
        {
            "hand_number": archive.record.hand_number,
            "ended_in_showdown": archive.record.ended_in_showdown,
            "replay_path": f"/table/{session.table_id}/replay/{archive.record.hand_number}",
        }
        for archive in reversed(session.orchestrator.completed_hand_archives)
    ]


def _serialize_showdown(showdown: WebShowdownState | None) -> dict[str, Any] | None:
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
    showdown: WebShowdownState | None,
) -> list[dict[str, Any]]:
    if showdown is not None:
        return [
            {
                "seat_id": winner.seat_id,
                "amount": winner.amount,
            }
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
        {
            "seat_id": seat.seat_id,
            "amount": seat.street_contribution,
        }
        for seat in public_view.seats
        if seat.street_contribution > 0
    ]


def _serialize_replay_seat_amount_badges(frame: ReplayFrame) -> list[dict[str, Any]]:
    if frame.winner_amounts:
        return [
            {
                "seat_id": seat_id,
                "amount": amount,
            }
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
        {
            "seat_id": seat.seat_id,
            "amount": seat.street_contribution,
        }
        for seat in frame.public_table_view.seats
        if seat.street_contribution > 0
    ]


def _event_kind(event: GameEvent) -> str:
    if event.event_type in {"pot_awarded", "hand_awarded", "chips_refunded"}:
        return "reward"
    if event.event_type in {"action_applied", "blind_posted", "showdown_revealed"}:
        return "action"
    return "state"


def _serialize_public_table(
    view: PublicTableView,
    *,
    human_seat_ids: set[str],
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
        "seats": [
            _serialize_seat(
                seat,
                is_human=seat.seat_id in human_seat_ids,
                is_viewer=seat.seat_id == viewer_seat_id,
            )
            for seat in view.seats
        ],
    }


def _serialize_seat(seat: SeatSnapshot, *, is_human: bool, is_viewer: bool) -> dict[str, Any]:
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
        "is_human": is_human,
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
