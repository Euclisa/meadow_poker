from __future__ import annotations

from html import escape

from poker_bot.types import (
    ActionType,
    DecisionRequest,
    GameEvent,
    LegalAction,
    PlayerUpdate,
    PlayerUpdateType,
    PlayerView,
    PublicTableView,
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
    legal_actions = ", ".join(_render_legal_action(item) for item in decision.legal_actions)
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


# ---------------------------------------------------------------------------
# CLI rendering
# ---------------------------------------------------------------------------

_SEPARATOR = "─" * 44


def render_cli_status(view: PlayerView) -> str:
    cards = _pretty_cards(view.hole_cards)
    board = _pretty_cards(view.public_table.board_cards)
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


def render_cli_standings(view: PublicTableView) -> str:
    ranked = sorted(view.seats, key=lambda s: s.stack, reverse=True)
    lines = [f"\n{_SEPARATOR}", "  Final standings", _SEPARATOR]
    for place, seat in enumerate(ranked, start=1):
        marker = "  *" if seat.stack > 0 else "   "
        lines.append(f"{marker} {place}. {seat.name:<20} {seat.stack:>6}")
    lines.append(_SEPARATOR)
    return "\n".join(lines)


def _render_cli_event(event: GameEvent, *, seat_names: dict[str, str]) -> str | None:
    payload = event.payload
    name = _seat_label(payload.get("seat_id"), seat_names)
    if event.event_type == "hand_started":
        return f"\n{_SEPARATOR}\n  Hand #{payload['hand_number']}\n{_SEPARATOR}"
    if event.event_type == "blind_posted":
        return f"  {name}: {payload['blind']} blind {payload['amount']}"
    if event.event_type == "street_started":
        phase = payload["phase"].replace("_", " ").title()
        board = _pretty_cards(tuple(payload.get("board_cards", ())))
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
        board = _pretty_cards(tuple(payload.get("board_cards", ())))
        return f"\n  --- Showdown: {board} ---"
    if event.event_type == "showdown_revealed":
        cards = _pretty_cards(tuple(payload.get("hole_cards", ())))
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


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_SUIT_SYMBOLS: dict[str, str] = {
    "s": "♠",
    "h": "♥",
    "d": "♦",
    "c": "♣",
}


def _pretty_card(card: str) -> str:
    if len(card) < 2:
        return card
    rank = card[:-1]
    suit = _SUIT_SYMBOLS.get(card[-1].lower(), card[-1])
    return f"{rank}{suit}"


def _pretty_cards(cards: tuple[str, ...]) -> str:
    if not cards:
        return "-"
    return " ".join(_pretty_card(c) for c in cards)


def _render_legal_action(action: LegalAction) -> str:
    if action.action_type in {ActionType.BET, ActionType.RAISE}:
        return f"{action.action_type.value}({action.min_amount}-{action.max_amount})"
    return action.action_type.value


def _render_event(event: GameEvent, *, seat_names: dict[str, str] | None = None) -> str | None:
    payload = event.payload
    name = _seat_label(payload.get("seat_id"), seat_names)
    if event.event_type == "action_applied":
        amount = payload.get("amount")
        if amount is None:
            return f"{name} -> {payload['action']}"
        return f"{name} -> {payload['action']} {amount}"
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
    if event.event_type == "table_completed":
        return f"Table completed ({payload.get('reason', 'unknown')})"
    if event.event_type == "chips_refunded":
        return f"{name} refunded {payload['amount']}"
    return None


def _seat_label(seat_id: str | None, seat_names: dict[str, str] | None) -> str:
    if seat_id is None:
        return "unknown"
    if seat_names is None:
        return seat_id
    return seat_names.get(seat_id, seat_id)


def _telegram_cards(cards: tuple[str, ...]) -> str:
    if not cards:
        return "-"
    return " ".join(escape(_pretty_card(card)) for card in cards)


def _render_telegram_event(
    event: GameEvent,
    *,
    seat_names: dict[str, str],
) -> tuple[str, str] | None:
    payload = event.payload
    name = _seat_label(payload.get("seat_id"), seat_names)
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
        cards = _telegram_cards(tuple(payload.get("hole_cards", ()))
        )
        return ("action", f"🂠 {styled_name} showed <code>{cards}</code> ({escape(payload['hand_label'])})")
    if event.event_type == "hand_completed":
        return ("state", f"✅ <b>Hand {payload['hand_number']}</b> completed")
    if event.event_type == "table_completed":
        return ("state", "🛑 <b>Table completed</b>")
    if event.event_type == "chips_refunded":
        return ("action", f"💰 {styled_name} refunded {payload['amount']}")
    if event.event_type == "bet_updated":
        return None
    return ("state", escape(_render_event(event, seat_names=seat_names)))
