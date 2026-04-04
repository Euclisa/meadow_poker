from __future__ import annotations

import pytest

from poker_bot.types import TelegramTableState
from poker_bot.web_app.registry import WebTableRegistry
from poker_bot.web_app.session import WebTableCreateRequest


def test_web_registry_create_join_rejoin_and_waiting_list() -> None:
    registry = WebTableRegistry()
    session, creator = registry.create_waiting_table(
        creator_name="Alice",
        request=WebTableCreateRequest(total_seats=3, llm_seat_count=1),
    )

    assert session.table_id
    assert creator.seat_id == "web_1"
    assert session.web_seat_count == 2
    assert registry.list_waiting_tables() == (session,)

    joined_session, bob = registry.join_table(table_id=session.table_id, display_name="Bob")
    assert joined_session is session
    assert bob.seat_id == "web_2"
    assert session.is_full() is True

    rejoined_session, rejoined_bob = registry.rejoin_table(table_id=session.table_id, seat_token=bob.seat_token)
    assert rejoined_session is session
    assert rejoined_bob.display_name == "Bob"

    with pytest.raises(ValueError, match="Display name is already taken"):
        registry.join_table(table_id=session.table_id, display_name="alice")


def test_web_registry_creator_cannot_leave_and_running_table_leaves_waiting_index() -> None:
    registry = WebTableRegistry()
    session, creator = registry.create_waiting_table(
        creator_name="Alice",
        request=WebTableCreateRequest(total_seats=2, llm_seat_count=0),
    )
    _session, bob = registry.join_table(table_id=session.table_id, display_name="Bob")

    with pytest.raises(ValueError, match="Creators must cancel"):
        registry.leave_waiting_table(table_id=session.table_id, seat_token=creator.seat_token)

    left_session = registry.leave_waiting_table(table_id=session.table_id, seat_token=bob.seat_token)
    assert left_session.human_player_count == 1

    registry.mark_running(session)
    assert session.status == TelegramTableState.RUNNING
    assert registry.list_waiting_tables() == ()

    registry.mark_completed(session)
    assert session.status == TelegramTableState.COMPLETED
