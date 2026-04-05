from __future__ import annotations

from poker_bot.types import GameEvent, HandRecord, HandRecordStatus, PlayerUpdate, PlayerView


def hand_record_from_updates(
    updates: list[PlayerUpdate],
    *,
    status: HandRecordStatus,
) -> HandRecord | None:
    if not updates:
        return None
    first_update = updates[0]
    final_update = updates[-1]
    events = tuple(event for update in updates for event in update.events)
    return HandRecord(
        hand_number=final_update.public_table_view.hand_number,
        status=status,
        events=events,
        start_public_view=first_update.public_table_view,
        current_public_view=final_update.public_table_view,
        ended_in_showdown=any(event.event_type == "showdown_started" for event in events),
    )


def render_public_completed_hand_summary(record: HandRecord) -> str:
    return _render_public_hand_summary(record, live=False)


def render_live_public_hand_summary(record: HandRecord) -> str:
    return _render_public_hand_summary(record, live=True)


def render_private_completed_hand_summary(record: HandRecord, player_view: PlayerView) -> str:
    public_summary = render_public_completed_hand_summary(record)
    perspective = [
        "Your perspective:",
        f"- You were: {player_view.player_name} at seat {player_view.seat_id}",
        f"- Your hole cards: {' '.join(player_view.hole_cards) or '-'}",
        f"- Your final stack: {player_view.stack}",
    ]
    return "\n".join([public_summary, "", *perspective])


def _render_public_hand_summary(record: HandRecord, *, live: bool) -> str:
    seat_names = {seat.seat_id: seat.name for seat in record.current_public_view.seats}
    sections: list[str] = []
    current_heading = "Preflop:"
    current_lines: list[str] = []
    showdown_lines: list[str] = []
    result_lines: list[str] = []

    def flush_current_section() -> None:
        nonlocal current_lines
        if current_lines:
            sections.append("\n".join([current_heading, *current_lines]))
            current_lines = []

    for event in record.events:
        payload = event.payload
        if event.event_type == "street_started":
            phase = payload["phase"]
            if phase == "preflop":
                continue
            flush_current_section()
            board = " ".join(payload.get("board_cards", ())) or "-"
            current_heading = f"{phase.title()}: {board}"
            continue
        if event.event_type == "showdown_started":
            flush_current_section()
            board = " ".join(payload.get("board_cards", ())) or "-"
            showdown_lines.append(f"- Final board: {board}")
            continue
        if event.event_type == "showdown_revealed":
            cards = " ".join(payload.get("hole_cards", ())) or "-"
            name = seat_names.get(payload.get("seat_id"), payload.get("seat_id", "unknown"))
            showdown_lines.append(f"- {name} showed {cards}: {payload['hand_label']}")
            continue
        if event.event_type in {"pot_awarded", "hand_awarded", "chips_refunded"}:
            result_lines.append(f"- {_render_summary_event(event, seat_names)}")
            continue
        if event.event_type in {"hand_started", "hand_completed", "table_completed", "bet_updated"}:
            continue
        rendered = _render_summary_event(event, seat_names)
        if rendered is not None:
            current_lines.append(f"- {rendered}")

    flush_current_section()

    lines = [
        f"Hand #{record.hand_number}",
        f"Status: {record.current_public_view.phase.value}",
        "",
        "Players at hand start:",
        *[
            f"- {seat.seat_id} {seat.name}: stack={seat.stack}"
            for seat in record.start_public_view.seats
        ],
    ]
    if sections:
        lines.extend(["", *sections])
    if not sections:
        lines.extend(
            [
                "",
                f"Board: {' '.join(record.current_public_view.board_cards) or '-'}",
                f"Pot: {record.current_public_view.pot_total}",
            ]
        )
    if showdown_lines:
        lines.extend(["", "Showdown:", *showdown_lines])
    if result_lines and not live:
        lines.extend(["", "Result:", *result_lines])
    stack_heading = "Current stacks:" if live else "Stacks after hand:"
    lines.extend(
        [
            "",
            stack_heading,
            *[
                f"- {seat.name}: {seat.stack}"
                for seat in record.current_public_view.seats
            ],
        ]
    )
    return "\n".join(lines)


def _render_summary_event(event: GameEvent, seat_names: dict[str, str]) -> str | None:
    payload = event.payload
    seat_id = payload.get("seat_id")
    name = seat_names.get(seat_id, seat_id or "unknown")
    if event.event_type == "blind_posted":
        return f"{name} posted {payload['blind']} blind {payload['amount']}"
    if event.event_type == "action_applied":
        amount = payload.get("amount")
        action = payload["action"]
        if action == "raise" and amount is not None:
            return f"{name} raised to {amount}"
        if action == "bet" and amount is not None:
            return f"{name} bet {amount}"
        if action == "call" and amount is not None:
            return f"{name} called {amount}"
        if action == "fold":
            return f"{name} folded"
        if action == "check":
            return f"{name} checked"
        if amount is not None:
            return f"{name} {action} {amount}"
        return f"{name} {action}"
    if event.event_type == "pot_awarded":
        return f"{name} won {payload['amount']}"
    if event.event_type == "hand_awarded":
        return f"{name} collected {payload['amount']}"
    if event.event_type == "chips_refunded":
        return f"{name} refunded {payload['amount']}"
    return None
