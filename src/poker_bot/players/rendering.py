from __future__ import annotations

from poker_bot.types import ActionType, DecisionRequest, GameEvent, LegalAction, PlayerView


def render_events(events: tuple[GameEvent, ...]) -> str:
    if not events:
        return "No new events."
    return "\n".join(_render_event(event) for event in events)


def render_decision_summary(decision: DecisionRequest) -> str:
    view = decision.player_view
    legal_actions = ", ".join(_render_legal_action(item) for item in decision.legal_actions)
    lines = [
        f"Seat: {view.seat_id} ({view.player_name})",
        f"Phase: {view.public_table.phase.value}",
        f"Hole cards: {' '.join(view.hole_cards) or '-'}",
        f"Board: {' '.join(view.public_table.board_cards) or '-'}",
        f"Pot: {view.public_table.pot_total}",
        f"Current bet: {view.public_table.current_bet}",
        f"To call: {view.to_call}",
        f"Stack: {view.stack}",
        f"Legal actions: {legal_actions}",
    ]
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


def _render_legal_action(action: LegalAction) -> str:
    if action.action_type in {ActionType.BET, ActionType.RAISE}:
        return f"{action.action_type.value}({action.min_amount}-{action.max_amount})"
    return action.action_type.value


def _render_event(event: GameEvent) -> str:
    payload = event.payload
    if event.event_type == "action_applied":
        amount = payload.get("amount")
        if amount is None:
            return f"{payload['seat_id']} -> {payload['action']}"
        return f"{payload['seat_id']} -> {payload['action']} {amount}"
    if event.event_type == "blind_posted":
        return f"{payload['seat_id']} posted {payload['blind']} blind {payload['amount']}"
    if event.event_type == "street_started":
        board = " ".join(payload.get("board_cards", ())) or "-"
        return f"{payload['phase']} started, board: {board}"
    if event.event_type == "pot_awarded":
        return f"{payload['seat_id']} won {payload['amount']}"
    if event.event_type == "hand_awarded":
        return f"{payload['seat_id']} collected {payload['amount']}"
    if event.event_type == "hand_started":
        return f"Hand {payload['hand_number']} started"
    if event.event_type == "hand_completed":
        return f"Hand {payload['hand_number']} completed"
    return f"{event.event_type}: {payload}"
