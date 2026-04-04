from __future__ import annotations

import asyncio
import pytest

from poker_bot.players.cli import CLIPlayerAgent
from poker_bot.players.llm import LLMGameClient, LLMPlayerAgent
from poker_bot.players.rendering import render_events
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
        self.messages_list: list[list[dict[str, str]]] = []

    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_output_tokens: int | None = None,
    ) -> object:
        self.messages_list.append(messages)
        message = type("Message", (), {"content": '{"action":"raise","amount":400}'})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


class FakeOpenAIClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": FakeResponsesAPI()})()


def test_llm_player_prompt_includes_recent_events_and_parses_json() -> None:
    decision = make_decision_request()
    client = FakeOpenAIClient()
    agent = LLMPlayerAgent(
        "p1",
        client=LLMGameClient(model="gpt-test", api_key="test", client=client),
    )

    action = asyncio.run(agent.request_action(decision))

    assert action == PlayerAction(ActionType.RAISE, amount=400)
    messages = client.chat.completions.messages_list[0]
    prompt = messages[-1]["content"]
    assert messages[0]["role"] == "developer"
    assert "Return exactly one JSON object and nothing else." in messages[0]["content"]
    assert "Recent events:" in prompt
    assert "p2 posted big blind 100" in prompt
    assert "raise total=200..1900" in prompt
    assert "Hole cards: As Kd" in prompt
    assert 'Invalid examples: Here is my move: {"action":"call"}' in prompt


class FallbackResponsesAPI:
    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_output_tokens: int | None = None,
    ) -> object:
        message = type(
            "Message",
            (),
            {"content": 'I will keep this short. {"action":"raise","amount":400} This is my move.'},
        )()
        choice = type("Choice", (), {"message": message})()
        return type(
            "Response",
            (),
            {"choices": [choice]},
        )()


class FallbackOpenAIClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": FallbackResponsesAPI()})()


def test_llm_client_extracts_first_json_object_from_mixed_text() -> None:
    client = LLMGameClient(model="gpt-test", api_key="test", client=FallbackOpenAIClient())

    completion = asyncio.run(client.complete_json([{"role": "user", "content": "prompt"}]))

    assert completion.payload == {"action": "raise", "amount": 400}


class ReasoningOnlyResponsesAPI:
    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_output_tokens: int | None = None,
    ) -> object:
        message = type("Message", (), {"content": None})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


class ReasoningOnlyOpenAIClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": ReasoningOnlyResponsesAPI()})()


def test_llm_client_rejects_reasoning_only_responses() -> None:
    client = LLMGameClient(model="gpt-test", api_key="test", client=ReasoningOnlyOpenAIClient())

    with pytest.raises(ValueError, match="text output"):
        asyncio.run(client.complete_json([{"role": "user", "content": "prompt"}]))


class RecordingCompletionsAPI:
    def __init__(self) -> None:
        self.calls: list[list[dict[str, str]]] = []

    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_output_tokens: int | None = None,
    ) -> object:
        self.calls.append(messages)
        message = type("Message", (), {"content": '{"action":"check"}'})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


class RecordingOpenAIClient:
    def __init__(self) -> None:
        self.chat = type("Chat", (), {"completions": RecordingCompletionsAPI()})()


def test_llm_player_agent_keeps_history_within_hand_and_resets_next_hand() -> None:
    client = RecordingOpenAIClient()
    agent = LLMPlayerAgent(
        "p1",
        client=LLMGameClient(model="gpt-test", api_key="test", client=client),
    )
    first = make_decision_request()
    second = DecisionRequest(
        acting_seat_id=first.acting_seat_id,
        player_view=PlayerView(
            seat_id="p1",
            player_name="Hero",
            hole_cards=("As", "Kd"),
            stack=1_900,
            contribution=100,
            position="dealer",
            to_call=0,
            public_table=PublicTableView(
                hand_number=1,
                phase=GamePhase.FLOP,
                board_cards=("2c", "7d", "8h"),
                pot_total=200,
                current_bet=0,
                dealer_seat_id="p1",
                acting_seat_id="p1",
                small_blind=50,
                big_blind=100,
                seats=first.public_table_view.seats,
            ),
        ),
        public_table_view=PublicTableView(
            hand_number=1,
            phase=GamePhase.FLOP,
            board_cards=("2c", "7d", "8h"),
            pot_total=200,
            current_bet=0,
            dealer_seat_id="p1",
            acting_seat_id="p1",
            small_blind=50,
            big_blind=100,
            seats=first.public_table_view.seats,
        ),
        legal_actions=(LegalAction(ActionType.CHECK),),
        recent_events=(GameEvent("street_started", {"phase": "flop", "board_cards": ("2c", "7d", "8h")}),),
    )
    third = DecisionRequest(
        acting_seat_id=first.acting_seat_id,
        player_view=PlayerView(
            seat_id="p1",
            player_name="Hero",
            hole_cards=("Qs", "Jh"),
            stack=1_900,
            contribution=50,
            position="dealer",
            to_call=50,
            public_table=PublicTableView(
                hand_number=2,
                phase=GamePhase.PREFLOP,
                board_cards=(),
                pot_total=150,
                current_bet=100,
                dealer_seat_id="p1",
                acting_seat_id="p1",
                small_blind=50,
                big_blind=100,
                seats=first.public_table_view.seats,
            ),
        ),
        public_table_view=PublicTableView(
            hand_number=2,
            phase=GamePhase.PREFLOP,
            board_cards=(),
            pot_total=150,
            current_bet=100,
            dealer_seat_id="p1",
            acting_seat_id="p1",
            small_blind=50,
            big_blind=100,
            seats=first.public_table_view.seats,
        ),
        legal_actions=(LegalAction(ActionType.FOLD), LegalAction(ActionType.CALL)),
        recent_events=(GameEvent("hand_started", {"hand_number": 2}),),
    )

    async def exercise() -> None:
        await agent.request_action(first)
        await agent.request_action(second)
        await agent.request_action(third)

    asyncio.run(exercise())

    calls = client.chat.completions.calls
    assert len(calls) == 3
    assert calls[0] == [
        {"role": "developer", "content": agent.system_prompt},
        {"role": "user", "content": calls[0][-1]["content"]},
    ]
    assert any(message["role"] == "assistant" for message in calls[1])
    assert calls[1][1]["role"] == "user"
    assert calls[1][2] == {"role": "assistant", "content": '{"action":"check"}'}
    assert calls[2] == [
        {"role": "developer", "content": agent.system_prompt},
        {"role": "user", "content": calls[2][-1]["content"]},
    ]


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


def test_telegram_player_agent_builds_reply_keyboard_from_legal_actions() -> None:
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
        handled = await agent.submit_text_action(user_id=99, chat_id=42, text="Call")
        assert handled is True
        return await pending

    action = asyncio.run(exercise_agent())

    assert action == PlayerAction(ActionType.CALL)
    assert sent_messages
    chat_id, text, keyboard = sent_messages[0]
    assert chat_id == 42
    assert "hand_started" not in text
    assert "Seat: p1" not in text
    assert "Player: Hero" in text
    assert "Legal actions:" not in text
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
        assert await agent.submit_text_action(user_id=99, chat_id=42, text="Raise 200-1900") is True
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
        assert await agent.submit_text_action(user_id=100, chat_id=42, text="Call") is False
        assert pending.done() is False
        assert await agent.submit_text_action(user_id=99, chat_id=42, text="Call") is True
        await pending

    asyncio.run(exercise_agent())
    assert sent_messages


def test_render_events_can_use_player_names_instead_of_seat_ids() -> None:
    events = (
        GameEvent("blind_posted", {"seat_id": "tg_1", "blind": "small", "amount": 50}),
        GameEvent("blind_posted", {"seat_id": "llm_1", "blind": "big", "amount": 100}),
        GameEvent("action_applied", {"seat_id": "tg_1", "action": "call", "amount": 50}),
    )

    rendered = render_events(events, seat_names={"tg_1": "Matvey Klochihin", "llm_1": "Nova_bot"})

    assert "Matvey Klochihin posted small blind 50" in rendered
    assert "Nova_bot posted big blind 100" in rendered
    assert "Matvey Klochihin -> call 50" in rendered
