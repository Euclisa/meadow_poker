from __future__ import annotations

from meadow.rendering.core import pretty_cards, seat_label
from meadow.types import DecisionRequest, GameEvent, PlayerUpdate, PublicTableView

_SEPARATOR = "─" * 44


def render_cli_status(view) -> str:
    cards = pretty_cards(view.hole_cards)
    board = pretty_cards(view.public_table.board_cards)
    lines = [
        f"  {cards}  |  {view.public_table.phase.value.replace('_', ' ').title()}",
        f"  Board: {board}",
        f"  Pot: {view.public_table.pot_total}  |  To call: {view.to_call}  |  Stack: {view.stack}",
    ]
    return "\n".join(lines)


def render_cli_turn_prompt(decision: DecisionRequest) -> str:
    parts: list[str] = []
    for action in decision.legal_actions:
        shortcut = action.action_type.value[0]
        label = action.action_type.value
        if action.min_amount is not None and action.max_amount is not None:
            if action.min_amount == action.max_amount:
                parts.append(f"[{shortcut}]{label[1:]} {action.min_amount}")
            else:
                parts.append(f"[{shortcut}]{label[1:]} {action.min_amount}-{action.max_amount}")
        else:
            parts.append(f"[{shortcut}]{label[1:]}")
    line = "  Actions: " + ", ".join(parts)
    lines = [line]
    if decision.turn_timeout_seconds is not None:
        lines.insert(0, f"  Turn timer: {decision.turn_timeout_seconds}s")
    if decision.validation_error is not None:
        lines.insert(0, f"  !! {decision.validation_error.message}")
    return "\n".join(lines)


def render_cli_events(update: PlayerUpdate) -> str:
    seat_names = {seat.seat_id: seat.name for seat in update.public_table_view.seats}
    lines: list[str] = []
    for event in update.events:
        rendered = _render_cli_event(event, seat_names=seat_names)
        if rendered is not None:
            lines.append(rendered)
    return "\n".join(lines)


def render_cli_public_events(events: tuple[GameEvent, ...], view: PublicTableView) -> str:
    seat_names = {seat.seat_id: seat.name for seat in view.seats}
    lines: list[str] = []
    for event in events:
        rendered = _render_cli_event(event, seat_names=seat_names)
        if rendered is not None:
            lines.append(rendered)
    return "\n".join(lines)


def render_cli_standings(view: PublicTableView) -> str:
    ranked = sorted(view.seats, key=lambda seat: seat.stack, reverse=True)
    lines = [f"\n{_SEPARATOR}", "  Final standings", _SEPARATOR]
    for place, seat in enumerate(ranked, start=1):
        marker = "  *" if seat.stack > 0 else "   "
        lines.append(f"{marker} {place}. {seat.name:<20} {seat.stack:>6}")
    lines.append(_SEPARATOR)
    return "\n".join(lines)


def _render_cli_event(event: GameEvent, *, seat_names: dict[str, str]) -> str | None:
    payload = event.payload
    name = seat_label(payload.get("seat_id"), seat_names)
    if event.event_type == "hand_started":
        return f"\n{_SEPARATOR}\n  Hand #{payload['hand_number']}\n{_SEPARATOR}"
    if event.event_type == "ante_posted":
        return f"  {name}: ante {payload['amount']}"
    if event.event_type == "blind_posted":
        return f"  {name}: {payload['blind']} blind {payload['amount']}"
    if event.event_type == "street_started":
        phase = payload["phase"].replace("_", " ").title()
        board = pretty_cards(tuple(payload.get("board_cards", ())))
        if board == "-":
            return f"\n  --- {phase} ---"
        return f"\n  --- {phase}: {board} ---"
    if event.event_type == "action_applied":
        action = payload["action"]
        amount = payload.get("amount")
        if action == "raise" and amount is not None:
            return f"  {name}: raise to {amount}"
        if action == "bet" and amount is not None:
            return f"  {name}: bet {amount}"
        if amount is not None:
            return f"  {name}: {action} {amount}"
        return f"  {name}: {action}"
    if event.event_type == "showdown_started":
        board = pretty_cards(tuple(payload.get("board_cards", ())))
        return f"\n  --- Showdown: {board} ---"
    if event.event_type == "showdown_revealed":
        cards = pretty_cards(tuple(payload.get("hole_cards", ())))
        return f"  {name}: showed {cards} ({payload['hand_label']})"
    if event.event_type == "pot_awarded":
        return f"  >> {name} wins {payload['amount']}"
    if event.event_type == "hand_awarded":
        return f"  >> {name} collects {payload['amount']}"
    if event.event_type == "hand_completed":
        return f"  Hand #{payload['hand_number']} complete"
    if event.event_type == "table_completed":
        reason = payload.get("reason", "unknown").replace("_", " ")
        return f"\n{_SEPARATOR}\n  Table finished ({reason})\n{_SEPARATOR}"
    if event.event_type == "chips_refunded":
        return f"  {name} refunded {payload['amount']}"
    if event.event_type == "bet_updated":
        return None
    return f"  {event.event_type}: {payload}"
