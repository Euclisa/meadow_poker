from __future__ import annotations

import asyncio

from poker_bot.config import CoachSettings, LLMSettings
from poker_bot.players.llm import LLMGameClient
from poker_bot.naming import BotNameAllocator
from poker_bot.telegram_app.app import TelegramApp, TelegramAppConfig
from poker_bot.types import TelegramTableState


class FakeResponsesAPI:
    def __init__(self, outputs: list[str], *, delay: float = 0.0) -> None:
        self.outputs = list(outputs)
        self.delay = delay
        self.messages_list: list[list[dict[str, str]]] = []

    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_output_tokens: int | None = None,
        extra_body: dict | None = None,
    ) -> object:
        self.messages_list.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        output = self.outputs.pop(0) if self.outputs else '{"action":"check"}'
        message = type("Message", (), {"content": output})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


class FakeOpenAIClient:
    def __init__(self, outputs: list[str], *, delay: float = 0.0) -> None:
        self.chat = type("Chat", (), {"completions": FakeResponsesAPI(outputs, delay=delay)})()


def make_app(
    *,
    max_hands: int | None = None,
    llm_outputs: list[str] | None = None,
    coach_outputs: list[str] | None = None,
    coach_delay: float = 0.0,
    llm_name_allocator: BotNameAllocator | None = None,
) -> tuple[TelegramApp, list[tuple[int, str, object | None]]]:
    sent_messages: list[tuple[int, str, object | None]] = []

    async def send_message(chat_id: int, text: str, reply_markup: object | None = None) -> None:
        sent_messages.append((chat_id, text, reply_markup))

    llm_outputs = llm_outputs or ['{"action":"check"}'] * 20

    def make_llm_client() -> LLMGameClient:
        return LLMGameClient(
            settings=LLMSettings(model="gpt-test", api_key="test"),
            client=FakeOpenAIClient(list(llm_outputs)),
        )

    def make_coach_client() -> LLMGameClient:
        return LLMGameClient(
            settings=CoachSettings(
                enabled=True,
                model="gpt-coach",
                api_key="coach-test",
                timeout=0.2,
            ),
            client=FakeOpenAIClient(list(coach_outputs or ["Coach reply"]), delay=coach_delay),
        )

    app = TelegramApp(
        TelegramAppConfig(
            bot_username="test_bot",
            llm=LLMSettings(model="gpt-test", api_key="test"),
            coach=CoachSettings(
                enabled=coach_outputs is not None,
                model="gpt-coach",
                api_key="coach-test",
                timeout=0.2,
            ),
            max_hands_per_table=max_hands,
        ),
        send_message=send_message,
        llm_client_factory=make_llm_client,
        coach_client_factory=make_coach_client,
        llm_name_allocator=llm_name_allocator,
    )
    return app, sent_messages


async def complete_create_flow(
    app: TelegramApp,
    *,
    user_id: int = 1,
    chat_id: int = 101,
    display_name: str = "Alice",
    total_seats: str = "2",
    llm_seats: str = "0",
    big_blind: str = "default",
    small_blind: str = "default",
    starting_stack: str = "default",
    turn_timeout: str = "default",
) -> None:
    await app.handle_create_table_command(user_id=user_id, chat_id=chat_id)
    await app.handle_text_message(user_id=user_id, chat_id=chat_id, display_name=display_name, text=total_seats)
    await app.handle_text_message(user_id=user_id, chat_id=chat_id, display_name=display_name, text=llm_seats)
    await app.handle_text_message(user_id=user_id, chat_id=chat_id, display_name=display_name, text=big_blind)
    await app.handle_text_message(user_id=user_id, chat_id=chat_id, display_name=display_name, text=small_blind)
    await app.handle_text_message(user_id=user_id, chat_id=chat_id, display_name=display_name, text=starting_stack)
    await app.handle_text_message(user_id=user_id, chat_id=chat_id, display_name=display_name, text=turn_timeout)


def test_create_table_guided_flow_and_creator_autojoin() -> None:
    app, messages = make_app()

    async def scenario() -> None:
        await complete_create_flow(app, total_seats="3", llm_seats="1")

    asyncio.run(scenario())

    table = app.registry.get_user_table(1)
    assert table is not None
    assert table.total_seats == 3
    assert table.llm_seat_count == 1
    assert table.telegram_seat_count == 2
    assert [user.user_id for user in table.claimed_telegram_users] == [1]
    assert any("Created table" in text for _chat, text, _markup in messages)
    assert any("Deep link:" in text for _chat, text, _markup in messages)
    assert any(markup == [["My Table"], ["Start Game"], ["Cancel Table"], ["Help"]] for _chat, _text, markup in messages)


def test_single_human_table_hides_join_info_and_announces_ready_to_start() -> None:
    app, messages = make_app()

    async def scenario() -> None:
        await complete_create_flow(app, total_seats="2", llm_seats="1")

    asyncio.run(scenario())

    created_messages = [item for item in messages if "Created table" in item[1]]
    assert created_messages
    _chat_id, text, markup = created_messages[-1]
    assert "Join with:" not in text
    assert "Deep link:" not in text
    assert "ready to start" in text
    assert "Press Start Game to begin." in text
    assert markup == [["Start Game"], ["My Table"], ["Cancel Table"], ["Help"]]


def test_multi_human_table_keeps_join_info_on_creation() -> None:
    app, messages = make_app()

    async def scenario() -> None:
        await complete_create_flow(app, total_seats="3", llm_seats="1")

    asyncio.run(scenario())

    created_messages = [item for item in messages if "Created table" in item[1]]
    assert created_messages
    _chat_id, text, _markup = created_messages[-1]
    assert "Join with:" in text
    assert "Deep link:" in text


def test_join_and_start_require_creator_and_full_human_seats() -> None:
    app, messages = make_app(max_hands=1)

    async def scenario() -> None:
        await complete_create_flow(app, total_seats="2", llm_seats="0")
        table = app.registry.get_user_table(1)
        assert table is not None
        await app.handle_start_game_command(user_id=1, chat_id=101)
        await app.handle_join_command(user_id=2, chat_id=202, display_name="Bob", table_id=table.table_id)
        await app.handle_start_game_command(user_id=2, chat_id=202)
        await app.handle_start_game_command(user_id=1, chat_id=101)
        await asyncio.sleep(0)
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="Fold")
        assert table.orchestrator_task is not None
        await table.orchestrator_task

    asyncio.run(scenario())

    texts = [text for _chat, text, _markup in messages]
    assert any("All Telegram seats must be claimed" in text for text in texts)
    assert any("joined table" in text for text in texts)
    assert any("Only the creator can start" in text for text in texts)
    table = next(iter(app.registry._tables.values()))
    assert table is not None
    assert table.status == TelegramTableState.COMPLETED


def test_ready_table_notification_emphasizes_start_button_for_creator() -> None:
    app, messages = make_app()

    async def scenario() -> None:
        await complete_create_flow(app, total_seats="2", llm_seats="0")
        table = app.registry.get_user_table(1)
        assert table is not None
        await app.handle_join_command(user_id=2, chat_id=202, display_name="Bob", table_id=table.table_id)

    asyncio.run(scenario())

    creator_messages = [(text, markup) for chat_id, text, markup in messages if chat_id == 101]
    assert any("ready to start" in text for text, _markup in creator_messages)
    assert any(markup == [["Start Game"], ["My Table"], ["Cancel Table"], ["Help"]] for _text, markup in creator_messages)
    joiner_messages = [(text, markup) for chat_id, text, markup in messages if chat_id == 202]
    assert any("ready to start" in text for text, _markup in joiner_messages)
    assert any(markup == [["My Table"], ["Leave Table"], ["Help"]] for _text, markup in joiner_messages)


def test_creator_leaving_waiting_table_cancels_it() -> None:
    app, messages = make_app()

    async def scenario() -> None:
        await complete_create_flow(app, total_seats="3", llm_seats="1")
        table = app.registry.get_user_table(1)
        assert table is not None
        await app.handle_join_command(user_id=2, chat_id=202, display_name="Bob", table_id=table.table_id)
        await app.handle_leave_table_command(user_id=1, chat_id=101)

    asyncio.run(scenario())

    table = next(iter(app.registry._tables.values()))
    assert table.status == TelegramTableState.CANCELLED
    assert app.registry.get_user_table(1) is None
    assert app.registry.get_user_table(2) is None
    assert any("cancelled" in text.lower() for _chat, text, _markup in messages)


def test_join_rejects_full_or_running_tables() -> None:
    app, messages = make_app(max_hands=1)

    async def scenario() -> None:
        await complete_create_flow(app, total_seats="2", llm_seats="0")
        table = app.registry.get_user_table(1)
        assert table is not None
        await app.handle_join_command(user_id=2, chat_id=202, display_name="Bob", table_id=table.table_id)
        await app.handle_start_game_command(user_id=1, chat_id=101)
        await asyncio.sleep(0)
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="Fold")
        assert table.orchestrator_task is not None
        await table.orchestrator_task
        await app.handle_join_command(user_id=3, chat_id=303, display_name="Cara", table_id=table.table_id)

    asyncio.run(scenario())

    texts = [text for _chat, text, _markup in messages]
    assert any("No open Telegram seats remain" in text or "Only waiting tables can be joined" in text for text in texts)


def test_mixed_table_can_complete_one_hand_and_unregister_users() -> None:
    app, messages = make_app(max_hands=1)

    async def scenario() -> None:
        await complete_create_flow(app, total_seats="2", llm_seats="1")
        table = app.registry.get_user_table(1)
        assert table is not None
        await app.handle_start_game_command(user_id=1, chat_id=101)
        await asyncio.sleep(0)
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="Fold")
        assert table.orchestrator_task is not None
        await table.orchestrator_task

    asyncio.run(scenario())

    assert app.registry.get_user_table(1) is None
    table = next(iter(app.registry._tables.values()))
    assert table.status == TelegramTableState.COMPLETED
    texts = [text for _chat, text, _markup in messages]
    assert any("started with 2 seats" in text for text in texts)
    assert any("has completed" in text for text in texts)


def test_telegram_coach_is_private_and_blocks_actions_while_pending() -> None:
    app, messages = make_app(max_hands=1, coach_outputs=["Coach reply"], coach_delay=0.05)

    async def scenario() -> None:
        await complete_create_flow(app, total_seats="2", llm_seats="0")
        table = app.registry.get_user_table(1)
        assert table is not None
        await app.handle_join_command(user_id=2, chat_id=202, display_name="Bob", table_id=table.table_id)
        await app.handle_start_game_command(user_id=1, chat_id=101)
        await asyncio.sleep(0.01)

        await app.handle_coach_command(user_id=2, chat_id=202, question="Any tips?")

        pending_coach = asyncio.create_task(
            app.handle_coach_command(
                user_id=1,
                chat_id=101,
                question="Should I fold here?",
            )
        )
        await asyncio.sleep(0.01)
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="Fold")
        await pending_coach
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="Fold")
        assert table.orchestrator_task is not None
        await table.orchestrator_task

    asyncio.run(scenario())

    texts = [text for _chat, text, _markup in messages]
    assert any("only available on your turn" in text for text in texts)
    assert any("Coach is thinking" in text for text in texts)
    assert any("Coach is still thinking" in text for text in texts)
    assert any(text == "Coach reply" for text in texts)


def test_telegram_human_and_llm_seat_names_are_assigned_cleanly() -> None:
    app, _messages = make_app(
        llm_name_allocator=BotNameAllocator(names=("Nova",), seed=1),
    )

    async def scenario() -> tuple[str, ...]:
        await complete_create_flow(
            app,
            display_name="Alice Wonder",
            total_seats="2",
            llm_seats="1",
        )
        table = app.registry.get_user_table(1)
        assert table is not None
        await app.handle_start_game_command(user_id=1, chat_id=101)
        assert table.engine is not None
        return tuple(seat.name for seat in table.engine.get_public_table_view().seats)

    seat_names = asyncio.run(scenario())

    assert "Alice Wonder" in seat_names
    assert "Nova_bot" in seat_names


def test_telegram_llm_seats_receive_recent_hand_count_config() -> None:
    app, _messages = make_app(
        llm_name_allocator=BotNameAllocator(names=("Nova",), seed=1),
    )
    app.config = TelegramAppConfig(
        bot_username="test_bot",
        llm=LLMSettings(model="gpt-test", api_key="test", recent_hand_count=7),
    )

    async def scenario() -> int:
        await complete_create_flow(app, total_seats="2", llm_seats="1")
        table = app.registry.get_user_table(1)
        assert table is not None
        await app.handle_start_game_command(user_id=1, chat_id=101)
        llm_agent = table.player_agents["llm_1"]
        return llm_agent.recent_hand_count

    recent_hand_count = asyncio.run(scenario())

    assert recent_hand_count == 7


def test_lobby_buttons_work_as_text_commands() -> None:
    app, messages = make_app()

    async def scenario() -> None:
        await app.handle_start_command(user_id=1, chat_id=101, display_name="Alice")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="Create Table")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="2")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="0")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="default")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="default")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="default")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="default")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="My Table")

    asyncio.run(scenario())

    assert any(markup == [["Create Table"], ["Help"]] for _chat, _text, markup in messages)
    assert any("Table " in text and "Status: waiting" in text for _chat, text, _markup in messages)


def test_help_works_during_create_flow() -> None:
    app, messages = make_app()

    async def scenario() -> None:
        await app.handle_create_table_command(user_id=1, chat_id=101)
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="Help")
        # Flow should still be active after help
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="2")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="0")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="default")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="default")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="default")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="default")

    asyncio.run(scenario())

    texts = [text for _chat, text, _markup in messages]
    assert any("/start" in text for text in texts)  # help text was shown
    table = app.registry.get_user_table(1)
    assert table is not None  # flow completed after help


def test_cancel_exits_create_flow() -> None:
    app, messages = make_app()

    async def scenario() -> None:
        await app.handle_create_table_command(user_id=1, chat_id=101)
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="cancel")

    asyncio.run(scenario())

    texts = [text for _chat, text, _markup in messages]
    assert any("Table creation cancelled" in text for text in texts)
    assert app.registry.get_user_table(1) is None
    assert 1 not in app._create_flows


def test_leave_notification_shows_display_name() -> None:
    app, messages = make_app()

    async def scenario() -> None:
        await complete_create_flow(app, total_seats="3", llm_seats="1")
        table = app.registry.get_user_table(1)
        assert table is not None
        await app.handle_join_command(user_id=2, chat_id=202, display_name="Bob", table_id=table.table_id)
        await app.handle_leave_table_command(user_id=2, chat_id=202)

    asyncio.run(scenario())

    texts = [text for _chat, text, _markup in messages]
    assert any("Bob left table" in text for text in texts)
    assert not any("User 2 left table" in text for text in texts)


def test_create_table_flow_accepts_custom_blinds_and_stack() -> None:
    app, messages = make_app()

    async def scenario() -> None:
        await complete_create_flow(
            app,
            total_seats="3",
            llm_seats="1",
            big_blind="200",
            small_blind="100",
            starting_stack="8000",
        )

    asyncio.run(scenario())

    table = app.registry.get_user_table(1)
    assert table is not None
    assert table.request.small_blind == 100
    assert table.request.big_blind == 200
    assert table.request.starting_stack == 8000
    texts = [text for _chat, text, _markup in messages]
    assert any("Blinds: 100/200" in text for text in texts)
    assert any("Starting stack: 8000" in text for text in texts)


def test_create_table_flow_accepts_turn_timeout() -> None:
    app, messages = make_app()

    async def scenario() -> None:
        await complete_create_flow(
            app,
            total_seats="2",
            llm_seats="0",
            turn_timeout="15",
        )

    asyncio.run(scenario())

    table = app.registry.get_user_table(1)
    assert table is not None
    assert table.request.turn_timeout_seconds == 15
    texts = [text for _chat, text, _markup in messages]
    assert any("Turn timer: 15s" in text for text in texts)


def test_telegram_turn_timeout_notifies_human_player() -> None:
    app, messages = make_app(max_hands=1)

    async def scenario() -> None:
        await complete_create_flow(app, total_seats="2", llm_seats="1", turn_timeout="1")
        table = app.registry.get_user_table(1)
        assert table is not None
        await app.handle_start_game_command(user_id=1, chat_id=101)
        assert table.orchestrator_task is not None
        await table.orchestrator_task

    asyncio.run(scenario())

    texts = [text for _chat, text, _markup in messages]
    assert any("Time expired. Auto-" in text for text in texts)
