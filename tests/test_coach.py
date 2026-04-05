from __future__ import annotations

import asyncio

import pytest

from poker_bot.coach import TableCoach
from poker_bot.config import CoachSettings
from poker_bot.players.llm import LLMGameClient
from poker_bot.types import (
    ActionType,
    DecisionRequest,
    GameEvent,
    GamePhase,
    HandRecord,
    HandRecordStatus,
    LegalAction,
    PlayerView,
    PublicTableView,
    SeatSnapshot,
)


class RecordingResponsesAPI:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)
        self.messages_list: list[list[dict[str, str]]] = []

    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_output_tokens: int | None = None,
        extra_body: dict | None = None,
    ) -> object:
        del model, max_output_tokens, extra_body
        self.messages_list.append(messages)
        output = self.outputs.pop(0)
        message = type("Message", (), {"content": output})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


class RecordingOpenAIClient:
    def __init__(self, outputs: list[str]) -> None:
        self.chat = type("Chat", (), {"completions": RecordingResponsesAPI(outputs)})()


def _make_public_view(*, hand_number: int, phase: GamePhase, board_cards: tuple[str, ...]) -> PublicTableView:
    return PublicTableView(
        hand_number=hand_number,
        phase=phase,
        board_cards=board_cards,
        pot_total=300,
        current_bet=100,
        dealer_seat_id="p1",
        acting_seat_id="p1" if phase != GamePhase.HAND_COMPLETE else None,
        small_blind=50,
        big_blind=100,
        seats=(
            SeatSnapshot("p1", "Hero", 1900, 100, False, False, True, "dealer"),
            SeatSnapshot("p2", "Villain", 2100, 100, False, False, True, "big_blind"),
        ),
    )


def _make_completed_record(hand_number: int) -> HandRecord:
    return HandRecord(
        hand_number=hand_number,
        status=HandRecordStatus.COMPLETED,
        events=(
            GameEvent("hand_started", {"hand_number": hand_number}),
            GameEvent("blind_posted", {"seat_id": "p1", "blind": "small", "amount": 50}),
            GameEvent("blind_posted", {"seat_id": "p2", "blind": "big", "amount": 100}),
            GameEvent("street_started", {"phase": "preflop", "board_cards": ()}),
            GameEvent("action_applied", {"seat_id": "p1", "action": "fold"}),
            GameEvent("hand_awarded", {"seat_id": "p2", "amount": 150}),
            GameEvent("hand_completed", {"hand_number": hand_number}),
        ),
        start_public_view=_make_public_view(hand_number=hand_number, phase=GamePhase.PREFLOP, board_cards=()),
        current_public_view=_make_public_view(
            hand_number=hand_number,
            phase=GamePhase.HAND_COMPLETE,
            board_cards=(),
        ),
        ended_in_showdown=False,
    )


def _make_live_record(hand_number: int) -> HandRecord:
    return HandRecord(
        hand_number=hand_number,
        status=HandRecordStatus.IN_PROGRESS,
        events=(
            GameEvent("hand_started", {"hand_number": hand_number}),
            GameEvent("blind_posted", {"seat_id": "p1", "blind": "small", "amount": 50}),
            GameEvent("blind_posted", {"seat_id": "p2", "blind": "big", "amount": 100}),
            GameEvent("street_started", {"phase": "preflop", "board_cards": ()}),
            GameEvent("action_applied", {"seat_id": "p1", "action": "call", "amount": 100}),
            GameEvent("action_applied", {"seat_id": "p2", "action": "check"}),
            GameEvent("street_started", {"phase": "flop", "board_cards": ("2c", "7d", "8h")}),
            GameEvent("action_applied", {"seat_id": "p2", "action": "bet", "amount": 100}),
        ),
        start_public_view=_make_public_view(hand_number=hand_number, phase=GamePhase.PREFLOP, board_cards=()),
        current_public_view=_make_public_view(
            hand_number=hand_number,
            phase=GamePhase.FLOP,
            board_cards=("2c", "7d", "8h"),
        ),
        ended_in_showdown=False,
    )


def _make_decision() -> DecisionRequest:
    public_view = _make_public_view(hand_number=3, phase=GamePhase.FLOP, board_cards=("2c", "7d", "8h"))
    player_view = PlayerView(
        seat_id="p1",
        player_name="Hero",
        hole_cards=("As", "Kd"),
        stack=1900,
        contribution=100,
        position="dealer",
        to_call=100,
        public_table=public_view,
    )
    return DecisionRequest(
        acting_seat_id="p1",
        player_view=player_view,
        public_table_view=public_view,
        legal_actions=(
            LegalAction(ActionType.FOLD),
            LegalAction(ActionType.CALL),
            LegalAction(ActionType.RAISE, min_amount=200, max_amount=1900),
        ),
    )


def test_table_coach_updates_public_note_and_advice_prompt_uses_only_note(caplog: pytest.LogCaptureFixture) -> None:
    client = RecordingOpenAIClient(["Public note v1", "Call looks fine here."])
    coach = TableCoach(
        LLMGameClient(
            settings=CoachSettings(enabled=True, model="gpt-test", api_key="test"),
            client=client,
        ),
        recent_hand_count=2,
    )

    async def scenario() -> str:
        await coach.record_completed_hand(_make_completed_record(1))
        await coach.record_completed_hand(_make_completed_record(2))
        return await coach.answer_question(
            table_id="table-1",
            seat_id="p1",
            decision=_make_decision(),
            current_hand_record=_make_live_record(3),
            question="Should I call or raise?",
        )

    with caplog.at_level("INFO"):
        reply = asyncio.run(scenario())

    assert reply == "Call looks fine here."
    note_prompt = "\n".join(item["content"] for item in client.chat.completions.messages_list[0])
    advice_prompt = "\n".join(item["content"] for item in client.chat.completions.messages_list[1])
    advice_instructions = client.chat.completions.messages_list[1][0]["content"]
    assert "Hand #1" in note_prompt
    assert "Hand #2" in note_prompt
    assert "Public note v1" in advice_prompt
    assert "Hand #3" in advice_prompt
    assert "As Kd" in advice_prompt
    assert "natural prose" in advice_instructions
    assert "no bullets" in advice_instructions
    assert "Hand #1" not in advice_prompt
    assert "Hand #2" not in advice_prompt
    assert any("reply=Call looks fine here." in record.getMessage() for record in caplog.records)
