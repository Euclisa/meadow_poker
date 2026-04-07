from __future__ import annotations

from meadow.types import (
    ActionType,
    DecisionRequest,
    GameEvent,
    LegalAction,
    PlayerUpdate,
    PlayerUpdateType,
    PlayerView,
)


def render_events(
    events: tuple[GameEvent, ...],
    *,
    seat_names: dict[str, str] | None = None,
) -> str:
    if not events:
        return "No new events."
    lines = [_render_event(event, seat_names=seat_names) for event in events]
    return "\n".join(line for line in lines if line is not None)


def render_decision_summary(
    decision: DecisionRequest,
    *,
    show_seat_id: bool = True,
    show_legal_actions: bool = True,
) -> str:
    view = decision.player_view
    legal_actions = ", ".join(render_legal_action(item) for item in decision.legal_actions)
    seat_line = f"Seat: {view.seat_id} ({view.player_name})" if show_seat_id else f"Player: {view.player_name}"
    lines = [
        seat_line,
        f"Phase: {view.public_table.phase.value}",
        f"Hole cards: {' '.join(view.hole_cards) or '-'}",
        f"Board: {' '.join(view.public_table.board_cards) or '-'}",
        f"Pot: {view.public_table.pot_total}",
        f"Current bet: {view.public_table.current_bet}",
        f"To call: {view.to_call}",
        f"Stack: {view.stack}",
    ]
    if show_legal_actions:
        lines.append(f"Legal actions: {legal_actions}")
    if decision.validation_error is not None:
        lines.append(f"Validation error: {decision.validation_error.message}")
    return "\n".join(lines)


def render_player_view(view: PlayerView) -> str:
    return "\n".join(
        [
            f"Seat: {view.seat_id} ({view.player_name})",
            f"Phase: {view.public_table.phase.value}",
            f"Board: {' '.join(view.public_table.board_cards) or '-'}",
            f"Hole cards: {' '.join(view.hole_cards) or '-'}",
            f"Pot: {view.public_table.pot_total}",
            f"Stack: {view.stack}",
        ]
    )


def render_player_update(update: PlayerUpdate, *, compact: bool = False) -> str:
    seat_names = {seat.seat_id: seat.name for seat in update.public_table_view.seats}
    event_text = render_events(update.events, seat_names=seat_names)
    if compact:
        lines = [event_text]
        if update.update_type == PlayerUpdateType.TURN_STARTED:
            actor = seat_names.get(update.acting_seat_id or "", "Unknown")
            lines.append(f"Now acting: {actor}")
        return "\n".join(line for line in lines if line)

    actor = seat_names.get(update.acting_seat_id or "", "Unknown") if update.acting_seat_id else "-"
    lines = [event_text]
    lines.append(f"Phase: {update.public_table_view.phase.value}")
    lines.append(f"Board: {' '.join(update.public_table_view.board_cards) or '-'}")
    lines.append(f"Pot: {update.public_table_view.pot_total}")
    lines.append(f"Now acting: {actor}")
    if update.is_your_turn:
        lines.append("It is your turn.")
    return "\n".join(lines)


_SUIT_SYMBOLS: dict[str, str] = {
    "s": "♠",
    "h": "♥",
    "d": "♦",
    "c": "♣",
}


def pretty_card(card: str) -> str:
    if len(card) < 2:
        return card
    rank = card[:-1]
    suit = _SUIT_SYMBOLS.get(card[-1].lower(), card[-1])
    return f"{rank}{suit}"


def pretty_cards(cards: tuple[str, ...]) -> str:
    if not cards:
        return "-"
    return " ".join(pretty_card(card) for card in cards)


def render_legal_action(action: LegalAction) -> str:
    if action.action_type in {ActionType.BET, ActionType.RAISE}:
        return f"{action.action_type.value}({action.min_amount}-{action.max_amount})"
    return action.action_type.value


def seat_label(seat_id: str | None, seat_names: dict[str, str] | None) -> str:
    if seat_id is None:
        return "unknown"
    if seat_names is None:
        return seat_id
    return seat_names.get(seat_id, seat_id)


def render_event(event: GameEvent, *, seat_names: dict[str, str] | None = None) -> str | None:
    return _render_event(event, seat_names=seat_names)


def _render_event(event: GameEvent, *, seat_names: dict[str, str] | None = None) -> str | None:
    payload = event.payload
    name = seat_label(payload.get("seat_id"), seat_names)
    if event.event_type == "action_applied":
        amount = payload.get("amount")
        if amount is None:
            return f"{name} -> {payload['action']}"
        return f"{name} -> {payload['action']} {amount}"
    if event.event_type == "ante_posted":
        return f"{name} posted ante {payload['amount']}"
    if event.event_type == "blind_posted":
        return f"{name} posted {payload['blind']} blind {payload['amount']}"
    if event.event_type == "street_started":
        board = " ".join(payload.get("board_cards", ())) or "-"
        return f"{payload['phase']} started, board: {board}"
    if event.event_type == "pot_awarded":
        return f"{name} won {payload['amount']}"
    if event.event_type == "hand_awarded":
        return f"{name} collected {payload['amount']}"
    if event.event_type == "hand_started":
        return f"Hand {payload['hand_number']} started"
    if event.event_type == "hand_completed":
        return f"Hand {payload['hand_number']} completed"
    if event.event_type == "showdown_started":
        board = " ".join(payload.get("board_cards", ())) or "-"
        return f"Showdown, board: {board}"
    if event.event_type == "showdown_revealed":
        cards = " ".join(payload.get("hole_cards", ())) or "-"
        return f"{name} showed {cards}: {payload['hand_label']}"
    if event.event_type == "seat_sat_out":
        return f"{name} is sitting out"
    if event.event_type == "seat_sat_in":
        return f"{name} is back in"
    if event.event_type == "table_paused":
        return "Table paused (waiting for players)"
    if event.event_type == "table_resumed":
        return "Table resumed"
    if event.event_type == "table_completed":
        return f"Table completed ({payload.get('reason', 'unknown')})"
    if event.event_type == "chips_refunded":
        return f"{name} refunded {payload['amount']}"
    return None
