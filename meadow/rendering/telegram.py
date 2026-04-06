from __future__ import annotations

from html import escape

from meadow.rendering.core import pretty_card, render_event, seat_label
from meadow.types import DecisionRequest, GameEvent, PlayerUpdate, PlayerView


def render_telegram_status_panel(view: PlayerView) -> str:
    return "\n".join(
        [
            f"<code>{_telegram_cards(view.hole_cards)}</code>  •  <b>{escape(view.public_table.phase.value.title())}</b>",
            f"🃏 Board: <code>{_telegram_cards(view.public_table.board_cards)}</code>",
            f"Pot: <b>{view.public_table.pot_total}</b>  •  To call: <b>{view.to_call}</b>  •  Stack: <b>{view.stack}</b>",
        ]
    )


def render_telegram_turn_prompt(decision: DecisionRequest) -> str:
    lines = ["👉 <b>Your move.</b>"]
    if decision.turn_timeout_seconds is not None:
        lines.append(f"⏳ {decision.turn_timeout_seconds}s turn timer.")
    if decision.player_view.to_call > 0:
        lines.append(f"Call: <b>{decision.player_view.to_call}</b>")
    else:
        lines.append("You can check or bet.")
    if decision.validation_error is not None:
        lines.append(f"⚠️ {escape(decision.validation_error.message)}")
    return "\n".join(lines)


def render_telegram_update_messages(update: PlayerUpdate) -> list[str]:
    seat_names = {seat.seat_id: seat.name for seat in update.public_table_view.seats}
    chunks: list[tuple[str, list[str]]] = []
    for event in update.events:
        rendered = _render_telegram_event(event, seat_names=seat_names)
        if rendered is None:
            continue
        kind, text = rendered
        if chunks and chunks[-1][0] == kind:
            chunks[-1][1].append(text)
        else:
            chunks.append((kind, [text]))
    return ["\n".join(lines) for _kind, lines in chunks]


def _telegram_cards(cards: tuple[str, ...]) -> str:
    if not cards:
        return "-"
    return " ".join(escape(pretty_card(card)) for card in cards)


def _render_telegram_event(
    event: GameEvent,
    *,
    seat_names: dict[str, str],
) -> tuple[str, str] | None:
    payload = event.payload
    name = seat_label(payload.get("seat_id"), seat_names)
    styled_name = f"<i>{escape(name)}</i>"
    if event.event_type == "action_applied":
        action = payload["action"]
        amount = payload.get("amount")
        if action == "raise" and amount is not None:
            return ("action", f"{styled_name}: raise to {amount}")
        if action == "bet" and amount is not None:
            return ("action", f"{styled_name}: bet {amount}")
        if amount is not None:
            return ("action", f"{styled_name}: {action} {amount}")
        return ("action", f"{styled_name}: {action}")
    if event.event_type == "ante_posted":
        return ("action", f"{styled_name}: ante {payload['amount']}")
    if event.event_type == "blind_posted":
        return ("action", f"{styled_name}: {payload['blind']} blind {payload['amount']}")
    if event.event_type == "pot_awarded":
        return ("action", f"🏆 {styled_name} wins {payload['amount']}")
    if event.event_type == "hand_awarded":
        return ("action", f"🏆 {styled_name} collects {payload['amount']}")
    if event.event_type == "hand_started":
        return ("state", f"🂠 <b>Hand {payload['hand_number']}</b> started")
    if event.event_type == "street_started":
        phase = payload["phase"].title()
        board_cards = tuple(payload.get("board_cards", ()))
        if board_cards:
            return ("state", f"🃏 <b>{escape(phase)}</b>: <code>{_telegram_cards(board_cards)}</code>")
        return ("state", f"🃏 <b>{escape(phase)}</b>")
    if event.event_type == "showdown_started":
        return ("state", f"🏁 <b>Showdown</b>: <code>{_telegram_cards(tuple(payload.get('board_cards', ())))}</code>")
    if event.event_type == "showdown_revealed":
        cards = _telegram_cards(tuple(payload.get("hole_cards", ())))
        return ("action", f"🂠 {styled_name} showed <code>{cards}</code> ({escape(payload['hand_label'])})")
    if event.event_type == "hand_completed":
        return ("state", f"✅ <b>Hand {payload['hand_number']}</b> completed")
    if event.event_type == "table_completed":
        return ("state", "🛑 <b>Table completed</b>")
    if event.event_type == "chips_refunded":
        return ("action", f"💰 {styled_name} refunded {payload['amount']}")
    if event.event_type == "bet_updated":
        return None
    fallback = render_event(event, seat_names=seat_names)
    if fallback is None:
        return None
    return ("state", escape(fallback))
