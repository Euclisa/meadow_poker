from __future__ import annotations

import asyncio
import pytest

from poker_bot.players.cli import CLIPlayerAgent
from poker_bot.players.llm import LLMGameClient, LLMPlayerAgent
from poker_bot.players.telegram import TelegramPlayerAgent
from poker_bot.types import (
    ActionType,
    DecisionRequest,
    GameEvent,
    GamePhase,
    LegalAction,
    PlayerAction,
    PlayerView,
    PublicTableView,
    SeatSnapshot,
)


def make_decision_request() -> DecisionRequest:
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
            SeatSnapshot("p1", "Hero", 1_900, 0, False, False, True, "dealer"),
            SeatSnapshot("p2", "Villain", 1_900, 100, False, False, True, "big_blind"),
        ),
    )
    player_view = PlayerView(
        seat_id="p1",
        player_name="Hero",
        hole_cards=("As", "Kd"),
        stack=1_900,
        contribution=0,
        position="dealer",
        to_call=100,
        public_table=public_table,
    )
    return DecisionRequest(
        acting_seat_id="p1",
        player_view=player_view,
        public_table_view=public_table,
        legal_actions=(
            LegalAction(ActionType.FOLD),
            LegalAction(ActionType.CALL),
            LegalAction(ActionType.RAISE, min_amount=200, max_amount=1_900),
        ),
        recent_events=(
            GameEvent("hand_started", {"hand_number": 1}),
            GameEvent("blind_posted", {"seat_id": "p2", "blind": "big", "amount": 100}),
        ),
    )


class FakeResponsesAPI:
    def __init__(self) -> None:
        self.prompts: list[str] = []

    async def create(
        self,
        *,
        model: str,
        input: str,
        max_output_tokens: int | None = None,
    ) -> object:
        self.prompts.append(input)
        return type("Response", (), {"output_text": '{"action":"raise","amount":400}'})()


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.responses = FakeResponsesAPI()


def test_llm_player_prompt_includes_recent_events_and_parses_json() -> None:
    decision = make_decision_request()
    client = FakeOpenAIClient()
    agent = LLMPlayerAgent(
        "p1",
        client=LLMGameClient(model="gpt-test", api_key="test", client=client),
    )

    action = asyncio.run(agent.request_action(decision))

    assert action == PlayerAction(ActionType.RAISE, amount=400)
    prompt = client.responses.prompts[0]
    assert "Recent events:" in prompt
    assert "p2 posted big blind 100" in prompt
    assert "raise total=200..1900" in prompt
    assert "Hole cards: As Kd" in prompt
    assert "Return exactly one JSON object and nothing else." in prompt
    assert 'Invalid examples: Here is my move: {"action":"call"}' in prompt


class FallbackResponsesAPI:
    async def create(
        self,
        *,
        model: str,
        input: str,
        max_output_tokens: int | None = None,
    ) -> object:
        return type(
            "Response",
            (),
            {
                "output_text": (
                    'I will keep this short. {"action":"raise","amount":400} This is my move.'
                )
            },
        )()


class FallbackOpenAIClient:
    def __init__(self) -> None:
        self.responses = FallbackResponsesAPI()


def test_llm_client_extracts_first_json_object_from_mixed_text() -> None:
    client = LLMGameClient(model="gpt-test", api_key="test", client=FallbackOpenAIClient())

    payload = asyncio.run(client.complete_json("prompt"))

    assert payload == {"action": "raise", "amount": 400}


class ReasoningOnlyResponsesAPI:
    async def create(
        self,
        *,
        model: str,
        input: str,
        max_output_tokens: int | None = None,
    ) -> object:
        reasoning_content = type(
            "Content",
            (),
            {"type": "reasoning_text", "text": 'I think {"action":"check"} is right.'},
        )()
        reasoning_item = type(
            "Item",
            (),
            {"type": "reasoning", "content": [reasoning_content]},
        )()
        return type(
            "Response",
            (),
            {
                "output_text": 'I think {"action":"check"} is right.',
                "output": [reasoning_item],
            },
        )()


class ReasoningOnlyOpenAIClient:
    def __init__(self) -> None:
        self.responses = ReasoningOnlyResponsesAPI()


def test_llm_client_rejects_reasoning_only_responses() -> None:
    client = LLMGameClient(model="gpt-test", api_key="test", client=ReasoningOnlyOpenAIClient())

    with pytest.raises(ValueError, match="non-reasoning output"):
        asyncio.run(client.complete_json("prompt"))


def test_cli_player_agent_uses_legal_actions_only() -> None:
    decision = make_decision_request()
    outputs: list[str] = []
    inputs = iter(["dance", "raise", "250"])
    agent = CLIPlayerAgent(
        "p1",
        input_func=lambda _: next(inputs),
        output_func=outputs.append,
    )

    action = asyncio.run(agent.request_action(decision))

    assert action == PlayerAction(ActionType.RAISE, amount=250)
    assert any("Illegal choice" in line for line in outputs)


def test_telegram_player_agent_builds_buttons_from_legal_actions() -> None:
    decision = make_decision_request()
    sent_messages: list[tuple[int, str, object | None]] = []

    async def send_message(chat_id: int, text: str, reply_markup: object | None) -> None:
        sent_messages.append((chat_id, text, reply_markup))

    agent = TelegramPlayerAgent(
        "p1",
        user_id=99,
        chat_id=42,
        send_message=send_message,
    )

    async def exercise_agent() -> PlayerAction:
        pending = asyncio.create_task(agent.request_action(decision))
        await asyncio.sleep(0)
        handled = await agent.submit_button_action(user_id=99, chat_id=42, action_name="call")
        assert handled is True
        return await pending

    action = asyncio.run(exercise_agent())

    assert action == PlayerAction(ActionType.CALL)
    assert sent_messages
    chat_id, text, keyboard = sent_messages[0]
    assert chat_id == 42
    assert "hand_started" not in text
    assert keyboard == ["Fold", "Call", "Raise 200-1900"]


def test_telegram_player_agent_bet_raise_requires_amount_and_validates_input() -> None:
    decision = make_decision_request()
    sent_messages: list[tuple[int, str, object | None]] = []

    async def send_message(chat_id: int, text: str, reply_markup: object | None) -> None:
        sent_messages.append((chat_id, text, reply_markup))

    agent = TelegramPlayerAgent(
        "p1",
        user_id=99,
        chat_id=42,
        send_message=send_message,
    )

    async def exercise_agent() -> PlayerAction:
        pending = asyncio.create_task(agent.request_action(decision))
        await asyncio.sleep(0)
        assert await agent.submit_button_action(user_id=99, chat_id=42, action_name="raise") is True
        assert await agent.submit_amount(user_id=99, chat_id=42, amount_text="abc") is True
        assert await agent.submit_amount(user_id=99, chat_id=42, amount_text="150") is True
        assert await agent.submit_amount(user_id=99, chat_id=42, amount_text="300") is True
        return await pending

    action = asyncio.run(exercise_agent())

    assert action == PlayerAction(ActionType.RAISE, amount=300)
    messages = [text for _chat_id, text, _reply_markup in sent_messages]
    assert any("Enter total amount for raise" in message for message in messages)
    assert any("Enter a numeric total amount." in message for message in messages)
    assert any("Amount must be at least 200." in message for message in messages)


def test_telegram_player_agent_rejects_wrong_user_input() -> None:
    decision = make_decision_request()
    sent_messages: list[tuple[int, str, object | None]] = []

    async def send_message(chat_id: int, text: str, reply_markup: object | None) -> None:
        sent_messages.append((chat_id, text, reply_markup))

    agent = TelegramPlayerAgent(
        "p1",
        user_id=99,
        chat_id=42,
        send_message=send_message,
    )

    async def exercise_agent() -> None:
        pending = asyncio.create_task(agent.request_action(decision))
        await asyncio.sleep(0)
        assert await agent.submit_button_action(user_id=100, chat_id=42, action_name="call") is False
        assert pending.done() is False
        assert await agent.submit_button_action(user_id=99, chat_id=42, action_name="call") is True
        await pending

    asyncio.run(exercise_agent())
    assert sent_messages
