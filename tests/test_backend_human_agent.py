from __future__ import annotations

import asyncio

from poker_bot.backend.service import BackendHumanAgent
from poker_bot.types import ActionType, DecisionRequest, LegalAction, PlayerAction, PlayerUpdate, PlayerUpdateType, PlayerView, PublicTableView, SeatSnapshot, GamePhase


def make_decision() -> DecisionRequest:
    public_table = PublicTableView(
        hand_number=1,
        phase=GamePhase.PREFLOP,
        board_cards=(),
        pot_total=150,
        current_bet=100,
        dealer_seat_id="p1",
        acting_seat_id="p1",
        small_blind=50,
        big_blind=100,
        seats=(
            SeatSnapshot("p1", "Alice", 1900, 50, False, False, True, "dealer"),
            SeatSnapshot("p2", "Bob", 1800, 100, False, False, True, "bb"),
        ),
    )
    player_view = PlayerView(
        seat_id="p1",
        player_name="Alice",
        hole_cards=("As", "Kd"),
        stack=1900,
        contribution=50,
        position="dealer",
        to_call=50,
        public_table=public_table,
    )
    return DecisionRequest(
        acting_seat_id="p1",
        player_view=player_view,
        public_table_view=public_table,
        legal_actions=(
            LegalAction(ActionType.FOLD),
            LegalAction(ActionType.CALL),
            LegalAction(ActionType.RAISE, min_amount=200, max_amount=400),
        ),
    )


def test_backend_human_agent_accepts_valid_actions_and_rejects_invalid() -> None:
    state_changes = 0

    async def on_state_changed() -> None:
        nonlocal state_changes
        state_changes += 1

    async def scenario() -> None:
        agent = BackendHumanAgent("p1", on_state_changed=on_state_changed)
        decision = make_decision()
        pending_task = asyncio.create_task(agent.request_action(decision))
        await asyncio.sleep(0)

        missing_amount = agent.submit_action(PlayerAction(ActionType.RAISE))
        assert missing_amount is not None
        assert missing_amount.code == "missing_amount"

        too_large = agent.submit_action(PlayerAction(ActionType.RAISE, amount=500))
        assert too_large is not None
        assert too_large.code == "amount_too_large"

        accepted = agent.submit_action(PlayerAction(ActionType.RAISE, amount=300))
        assert accepted is None
        resolved = await pending_task
        assert resolved == PlayerAction(ActionType.RAISE, amount=300)

        await agent.close()

    asyncio.run(scenario())
    assert state_changes >= 2


def test_backend_human_agent_clears_pending_state_on_update() -> None:
    async def on_state_changed() -> None:
        return None

    async def scenario() -> None:
        agent = BackendHumanAgent("p1", on_state_changed=on_state_changed)
        decision = make_decision()
        pending_task = asyncio.create_task(agent.request_action(decision))
        await asyncio.sleep(0)
        assert agent.pending_decision is not None

        await agent.notify_update(
            PlayerUpdate(
                update_type=PlayerUpdateType.STATE_CHANGED,
                events=(),
                public_table_view=decision.public_table_view,
                player_view=decision.player_view,
                acting_seat_id=None,
                is_your_turn=False,
            )
        )
        assert agent.pending_decision is None
        await agent.close()
        pending_task.cancel()

    asyncio.run(scenario())
