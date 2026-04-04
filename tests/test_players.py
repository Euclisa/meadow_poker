from __future__ import annotations

import asyncio
import pytest

from poker_bot.players.cli import CLIPlayerAgent
from poker_bot.players.llm import LLMGameClient, LLMPlayerAgent
from poker_bot.players.rendering import (
    render_events,
    render_player_update,
    render_telegram_status_panel,
    render_telegram_update_messages,
)
from poker_bot.players.telegram import TelegramPlayerAgent
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
    )


def make_player_update(*, is_your_turn: bool = False) -> PlayerUpdate:
    decision = make_decision_request()
    return PlayerUpdate(
        update_type=PlayerUpdateType.TURN_STARTED if is_your_turn else PlayerUpdateType.STATE_CHANGED,
        events=(
            GameEvent("hand_started", {"hand_number": 1}),
            GameEvent("blind_posted", {"seat_id": "p2", "blind": "big", "amount": 100}),
        ),
        public_table_view=decision.public_table_view,
        player_view=decision.player_view,
        acting_seat_id=decision.acting_seat_id,
        is_your_turn=is_your_turn,
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


def test_llm_player_prompt_includes_buffered_updates_and_parses_json() -> None:
    decision = make_decision_request()
    client = FakeOpenAIClient()
    agent = LLMPlayerAgent(
        "p1",
        client=LLMGameClient(model="gpt-test", api_key="test", client=client),
    )

    async def exercise() -> PlayerAction:
        await agent.notify_update(make_player_update())
        return await agent.request_action(decision)

    action = asyncio.run(exercise())

    assert action == PlayerAction(ActionType.RAISE, amount=400)
    messages = client.chat.completions.messages_list[0]
    prompt = messages[-1]["content"]
    assert messages[0]["role"] == "developer"
    assert "Return exactly one JSON object and nothing else." in messages[0]["content"]
    assert "Updates since your last turn:" in prompt
    assert "Villain posted big blind 100" in prompt
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
    )

    async def exercise() -> None:
        await agent.request_action(first)
        await agent.notify_update(
            PlayerUpdate(
                update_type=PlayerUpdateType.STATE_CHANGED,
                events=(GameEvent("street_started", {"phase": "flop", "board_cards": ("2c", "7d", "8h")}),),
                public_table_view=second.public_table_view,
                player_view=second.player_view,
                acting_seat_id=second.acting_seat_id,
                is_your_turn=True,
            )
        )
        await agent.request_action(second)
        await agent.notify_update(
            PlayerUpdate(
                update_type=PlayerUpdateType.HAND_COMPLETED,
                events=(GameEvent("hand_completed", {"hand_number": 1}),),
                public_table_view=second.public_table_view,
                player_view=second.player_view,
                acting_seat_id=None,
                is_your_turn=False,
            )
        )
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
    assert "flop started, board: 2c 7d 8h" in calls[1][-1]["content"]
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


def test_cli_player_agent_accepts_shortcuts() -> None:
    decision = make_decision_request()
    outputs: list[str] = []
    inputs = iter(["f"])
    agent = CLIPlayerAgent(
        "p1",
        input_func=lambda _: next(inputs),
        output_func=outputs.append,
    )

    action = asyncio.run(agent.request_action(decision))

    assert action == PlayerAction(ActionType.FOLD)


def test_cli_player_agent_validates_amount_range() -> None:
    decision = make_decision_request()
    outputs: list[str] = []
    inputs = iter(["r", "abc", "r", "50", "r", "300"])
    agent = CLIPlayerAgent(
        "p1",
        input_func=lambda _: next(inputs),
        output_func=outputs.append,
    )

    action = asyncio.run(agent.request_action(decision))

    assert action == PlayerAction(ActionType.RAISE, amount=300)
    assert any("Enter a number" in line for line in outputs)
    assert any("Minimum" in line for line in outputs)


def test_cli_player_agent_auto_fills_all_in_amount() -> None:
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
            SeatSnapshot("p1", "Hero", 180, 0, False, False, True, "dealer"),
            SeatSnapshot("p2", "Villain", 1_900, 100, False, False, True, "big_blind"),
        ),
    )
    player_view = PlayerView(
        seat_id="p1",
        player_name="Hero",
        hole_cards=("As", "Kd"),
        stack=180,
        contribution=0,
        position="dealer",
        to_call=100,
        public_table=public_table,
    )
    decision = DecisionRequest(
        acting_seat_id="p1",
        player_view=player_view,
        public_table_view=public_table,
        legal_actions=(
            LegalAction(ActionType.FOLD),
            LegalAction(ActionType.CALL),
            LegalAction(ActionType.RAISE, min_amount=180, max_amount=180),
        ),
    )
    outputs: list[str] = []
    inputs = iter(["r"])
    agent = CLIPlayerAgent(
        "p1",
        input_func=lambda _: next(inputs),
        output_func=outputs.append,
    )

    action = asyncio.run(agent.request_action(decision))

    assert action == PlayerAction(ActionType.RAISE, amount=180)
    assert any("All-in: 180" in line for line in outputs)


def test_cli_notify_update_renders_events_with_unicode_cards() -> None:
    decision = make_decision_request()
    update = PlayerUpdate(
        update_type=PlayerUpdateType.STATE_CHANGED,
        events=(
            GameEvent("hand_started", {"hand_number": 1}),
            GameEvent("blind_posted", {"seat_id": "p1", "blind": "small", "amount": 50}),
            GameEvent("street_started", {"phase": "flop", "board_cards": ("As", "Kh", "Qd")}),
        ),
        public_table_view=decision.public_table_view,
        player_view=decision.player_view,
        acting_seat_id="p1",
        is_your_turn=False,
    )
    outputs: list[str] = []
    agent = CLIPlayerAgent("p1", output_func=outputs.append)

    asyncio.run(agent.notify_update(update))

    rendered = "\n".join(outputs)
    assert "Hand #1" in rendered
    assert "A♠ K♥ Q♦" in rendered
    assert "Flop" in rendered


def test_cli_status_shows_unicode_cards_and_state() -> None:
    decision = make_decision_request()
    outputs: list[str] = []
    inputs = iter(["f"])
    agent = CLIPlayerAgent(
        "p1",
        input_func=lambda _: next(inputs),
        output_func=outputs.append,
    )

    asyncio.run(agent.request_action(decision))

    rendered = "\n".join(outputs)
    assert "A♠ K♦" in rendered
    assert "Pot: 150" in rendered
    assert "Stack: 1900" in rendered
    assert "[f]old" in rendered


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
    assert "A♠ K♦" in text
    assert "Pot: <b>150</b>" in text
    assert "Stack: <b>1900</b>" in text
    assert keyboard is None
    _chat_id, prompt_text, prompt_keyboard = sent_messages[1]
    assert "Your move" in prompt_text
    assert prompt_keyboard == ["Fold", "Call", "Raise 200-1900"]


def test_telegram_player_agent_sends_immediate_update_messages() -> None:
    sent_messages: list[tuple[int, str, object | None]] = []

    async def send_message(chat_id: int, text: str, reply_markup: object | None) -> None:
        sent_messages.append((chat_id, text, reply_markup))

    agent = TelegramPlayerAgent(
        "p1",
        user_id=99,
        chat_id=42,
        send_message=send_message,
    )

    asyncio.run(agent.notify_update(make_player_update()))

    assert len(sent_messages) >= 3
    assert "A♠ K♦" in sent_messages[0][1]
    assert "Hand 1" in sent_messages[1][1]
    assert "<i>Villain</i>: big blind 100" in sent_messages[2][1]


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


def test_render_player_update_marks_turn_started() -> None:
    rendered = render_player_update(make_player_update(is_your_turn=True))

    assert "It is your turn." in rendered


def test_render_telegram_status_panel_formats_cards_with_unicode_suits() -> None:
    rendered = render_telegram_status_panel(make_decision_request().player_view)

    assert "As Kd" not in rendered
    assert "A♠ K♦" in rendered
    assert "Current bet:" not in rendered
    assert "Street:" not in rendered


def test_render_telegram_update_messages_split_state_and_actions() -> None:
    messages = render_telegram_update_messages(make_player_update())

    assert len(messages) == 2
    assert "Hand 1" in messages[0]
    assert "<i>Villain</i>: big blind 100" in messages[1]


def test_render_telegram_update_messages_preserve_event_order_across_kinds() -> None:
    decision = make_decision_request()
    update = PlayerUpdate(
        update_type=PlayerUpdateType.TURN_STARTED,
        events=(
            GameEvent("action_applied", {"seat_id": "p1", "action": "call", "amount": 100}),
            GameEvent("street_started", {"phase": "flop", "board_cards": ("3h", "4h", "Kd")}),
            GameEvent("action_applied", {"seat_id": "p2", "action": "check"}),
        ),
        public_table_view=decision.public_table_view,
        player_view=decision.player_view,
        acting_seat_id="p1",
        is_your_turn=True,
    )

    messages = render_telegram_update_messages(update)

    assert messages == [
        "<i>Hero</i>: call 100",
        "🃏 <b>Flop</b>: <code>3♥ 4♥ K♦</code>",
        "<i>Villain</i>: check",
    ]


def test_telegram_player_agent_does_not_duplicate_status_before_turn_prompt() -> None:
    base_decision = make_decision_request()
    turn_public = PublicTableView(
        hand_number=1,
        phase=GamePhase.FLOP,
        board_cards=("3h", "4h", "Kd"),
        pot_total=500,
        current_bet=0,
        dealer_seat_id="p1",
        acting_seat_id="p1",
        small_blind=50,
        big_blind=100,
        seats=base_decision.public_table_view.seats,
    )
    decision = DecisionRequest(
        acting_seat_id="p1",
        player_view=PlayerView(
            seat_id="p1",
            player_name="Hero",
            hole_cards=("8c", "Jc"),
            stack=1_800,
            contribution=200,
            position="dealer",
            to_call=0,
            public_table=turn_public,
        ),
        public_table_view=turn_public,
        legal_actions=(LegalAction(ActionType.CHECK), LegalAction(ActionType.BET, min_amount=100, max_amount=1800)),
    )
    sent_messages: list[tuple[int, str, object | None]] = []

    async def send_message(chat_id: int, text: str, reply_markup: object | None) -> None:
        sent_messages.append((chat_id, text, reply_markup))

    agent = TelegramPlayerAgent(
        "p1",
        user_id=99,
        chat_id=42,
        send_message=send_message,
    )

    async def exercise() -> None:
        await agent.notify_update(
            PlayerUpdate(
                update_type=PlayerUpdateType.TURN_STARTED,
                    events=(
                        GameEvent("action_applied", {"seat_id": "p1", "action": "call", "amount": 100}),
                        GameEvent("street_started", {"phase": "flop", "board_cards": ("3h", "4h", "Kd")}),
                        GameEvent("action_applied", {"seat_id": "p2", "action": "check"}),
                    ),
                    public_table_view=turn_public,
                    player_view=decision.player_view,
                    acting_seat_id="p1",
                    is_your_turn=True,
                )
            )
        pending = asyncio.create_task(agent.request_action(decision))
        await asyncio.sleep(0)
        await agent.submit_text_action(user_id=99, chat_id=42, text="Check")
        await pending

    asyncio.run(exercise())

    status_messages = [text for _chat_id, text, _markup in sent_messages if "8♣ J♣" in text]
    prompt_messages = [text for _chat_id, text, _markup in sent_messages if "Your move" in text]
    assert len(status_messages) == 1, f"Expected 1 status panel, got {len(status_messages)}: {status_messages}"
    assert len(prompt_messages) == 1


def test_telegram_player_agent_cancel_exits_amount_entry() -> None:
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
        # Enter raise amount flow
        assert await agent.submit_text_action(user_id=99, chat_id=42, text="Raise 200-1900") is True
        # Cancel out of amount entry
        assert await agent.submit_text_action(user_id=99, chat_id=42, text="cancel") is True
        # Should be back at action selection — now fold instead
        assert await agent.submit_text_action(user_id=99, chat_id=42, text="Fold") is True
        return await pending

    action = asyncio.run(exercise_agent())

    assert action == PlayerAction(ActionType.FOLD)
    messages = [text for _chat_id, text, _markup in sent_messages]
    assert any("Amount entry cancelled" in m for m in messages)


def test_render_events_handles_chips_refunded() -> None:
    events = (
        GameEvent("chips_refunded", {"seat_id": "p1", "amount": 200}),
    )

    rendered = render_events(events, seat_names={"p1": "Hero"})

    assert "Hero refunded 200" in rendered


def test_render_events_handles_showdown_and_table_completed() -> None:
    events = (
        GameEvent("showdown_started", {"board_cards": ("As", "Kh", "Qd", "Jc", "Tc")}),
        GameEvent("table_completed", {"reason": "not_enough_players", "hand_number": 1}),
    )

    rendered = render_events(events)

    assert "Showdown, board: As Kh Qd Jc Tc" in rendered
    assert "Table completed (not_enough_players)" in rendered


def test_render_telegram_chips_refunded_event() -> None:
    decision = make_decision_request()
    update = PlayerUpdate(
        update_type=PlayerUpdateType.TABLE_COMPLETED,
        events=(
            GameEvent("chips_refunded", {"seat_id": "p1", "amount": 200}),
            GameEvent("table_completed", {"reason": "deck_exhausted", "hand_number": 1}),
        ),
        public_table_view=decision.public_table_view,
        player_view=decision.player_view,
        acting_seat_id=None,
        is_your_turn=False,
    )

    messages = render_telegram_update_messages(update)

    refund_msg = next((m for m in messages if "refunded" in m), None)
    assert refund_msg is not None
    assert "💰" in refund_msg
    assert "200" in refund_msg
