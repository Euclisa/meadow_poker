from __future__ import annotations

import asyncio

import pytest

from poker_bot.types import (
    ActionType,
    DecisionRequest,
    GameEvent,
    GamePhase,
    LegalAction,
    PlayerAction,
    PlayerUpdate,
    PlayerUpdateType,
    PlayerView,
    PublicTableView,
    SeatSnapshot,
)
from poker_bot.web_app.player import WebPlayerAgent


def make_decision_request() -> DecisionRequest:
    public_table = PublicTableView(
        hand_number=1,
        phase=GamePhase.PREFLOP,
        board_cards=(),
        pot_total=150,
        current_bet=100,
        dealer_seat_id="web_1",
        acting_seat_id="web_1",
        small_blind=50,
        big_blind=100,
        seats=(
            SeatSnapshot("web_1", "Hero", 1_900, 0, False, False, True, "dealer"),
            SeatSnapshot("web_2", "Villain", 1_900, 100, False, False, True, "big_blind"),
        ),
    )
    player_view = PlayerView(
        seat_id="web_1",
        player_name="Hero",
        hole_cards=("As", "Kd"),
        stack=1_900,
        contribution=0,
        position="dealer",
        to_call=100,
        public_table=public_table,
    )
    return DecisionRequest(
        acting_seat_id="web_1",
        player_view=player_view,
        public_table_view=public_table,
        legal_actions=(
            LegalAction(ActionType.FOLD),
            LegalAction(ActionType.CALL),
            LegalAction(ActionType.RAISE, min_amount=200, max_amount=1_900),
        ),
    )


def make_player_update() -> PlayerUpdate:
    decision = make_decision_request()
    return PlayerUpdate(
        update_type=PlayerUpdateType.STATE_CHANGED,
        events=(),
        public_table_view=decision.public_table_view,
        player_view=decision.player_view,
        acting_seat_id=None,
        is_your_turn=False,
    )


def test_web_player_agent_accepts_valid_action_and_clears_on_update() -> None:
    publishes: list[str] = []

    async def publish() -> None:
        publishes.append("tick")

    agent = WebPlayerAgent("web_1", publish_state=publish)
    decision = make_decision_request()

    async def scenario() -> None:
        task = asyncio.create_task(agent.request_action(decision))
        await asyncio.sleep(0)
        assert agent.pending_decision == decision

        error = agent.submit_action(PlayerAction(ActionType.CALL))
        assert error is None
        assert await task == PlayerAction(ActionType.CALL)

        await agent.notify_update(make_player_update())
        assert agent.pending_decision is None

    asyncio.run(scenario())

    assert len(publishes) >= 2


def test_web_player_agent_rejects_invalid_raise_amount_without_finishing_turn() -> None:
    agent = WebPlayerAgent("web_1")
    decision = make_decision_request()

    async def scenario() -> None:
        task = asyncio.create_task(agent.request_action(decision))
        await asyncio.sleep(0)

        error = agent.submit_action(PlayerAction(ActionType.RAISE, amount=150))
        assert error is not None
        assert error.code == "amount_too_small"
        assert task.done() is False

        await agent.cancel_pending_action("test_complete")
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


def test_web_player_agent_can_skip_publish_for_showdown_hand_completion() -> None:
    publishes: list[str] = []

    async def publish() -> None:
        publishes.append("tick")

    def should_publish(update: PlayerUpdate) -> bool:
        return not (
            update.update_type == PlayerUpdateType.HAND_COMPLETED
            and any(event.event_type == "showdown_started" for event in update.events)
        )

    agent = WebPlayerAgent("web_1", publish_state=publish, should_publish_update=should_publish)
    decision = make_decision_request()
    showdown_update = PlayerUpdate(
        update_type=PlayerUpdateType.HAND_COMPLETED,
        events=(
            GameEvent("showdown_started", {"board_cards": ("As", "Kh", "Qd", "Jc", "Tc")}),
            GameEvent("showdown_revealed", {"seat_id": "web_1", "hole_cards": ("As", "Kd"), "hand_label": "straight"}),
            GameEvent("pot_awarded", {"seat_id": "web_1", "amount": 400}),
            GameEvent("hand_completed", {"hand_number": 1}),
        ),
        public_table_view=decision.public_table_view,
        player_view=decision.player_view,
        acting_seat_id=None,
        is_your_turn=False,
    )

    async def scenario() -> None:
        task = asyncio.create_task(agent.request_action(decision))
        await asyncio.sleep(0)

        error = agent.submit_action(PlayerAction(ActionType.CALL))
        assert error is None
        assert await task == PlayerAction(ActionType.CALL)

        await agent.notify_update(showdown_update)
        assert agent.pending_decision is None

    asyncio.run(scenario())

    assert publishes == ["tick"]
