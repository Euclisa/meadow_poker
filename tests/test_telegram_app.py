from __future__ import annotations

import asyncio

from meadow.config import CoachSettings, LLMSettings
from meadow.naming import BotNameAllocator
from meadow.telegram_app.app import TelegramApp, TelegramAppConfig

from support import make_backend_service, make_http_backend_client


def make_remote_app(backend) -> tuple[TelegramApp, list[tuple[int, str, object | None]]]:
    sent_messages: list[tuple[int, str, object | None]] = []

    async def send_message(chat_id: int, text: str, reply_markup: object | None = None) -> None:
        sent_messages.append((chat_id, text, reply_markup))

    app = TelegramApp(
        TelegramAppConfig(
            bot_username="test_bot",
            llm=LLMSettings(model="gpt-test", api_key="test"),
            coach=CoachSettings(enabled=False),
            max_hands_per_table=1,
        ),
        send_message=send_message,
        llm_name_allocator=BotNameAllocator(names=("Nova",), seed=1),
        backend=backend,
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
    ante: str = "default",
    starting_stack: str = "default",
    turn_timeout: str = "30",
    idle_close: str = "300",
) -> None:
    await app.handle_create_table_command(user_id=user_id, chat_id=chat_id)
    await app.handle_text_message(user_id=user_id, chat_id=chat_id, display_name=display_name, text=total_seats)
    await app.handle_text_message(user_id=user_id, chat_id=chat_id, display_name=display_name, text=llm_seats)
    await app.handle_text_message(user_id=user_id, chat_id=chat_id, display_name=display_name, text=big_blind)
    await app.handle_text_message(user_id=user_id, chat_id=chat_id, display_name=display_name, text=small_blind)
    await app.handle_text_message(user_id=user_id, chat_id=chat_id, display_name=display_name, text=ante)
    await app.handle_text_message(user_id=user_id, chat_id=chat_id, display_name=display_name, text=starting_stack)
    await app.handle_text_message(user_id=user_id, chat_id=chat_id, display_name=display_name, text=turn_timeout)
    await app.handle_text_message(user_id=user_id, chat_id=chat_id, display_name=display_name, text=idle_close)


def test_remote_telegram_create_join_and_start_use_backend_state_for_keyboards() -> None:
    async def scenario() -> None:
        service = make_backend_service()
        app, messages = make_remote_app(make_http_backend_client(service))
        await complete_create_flow(app, total_seats="2", llm_seats="0")

        created_messages = [item for item in messages if "Created table" in item[1]]
        assert created_messages
        assert "Turn timer: 30s" in created_messages[-1][1]
        assert "Idle close: 300s" in created_messages[-1][1]
        assert created_messages[-1][2] == [["My Table"], ["Start Game"], ["Cancel Table"], ["Help"]]

        table_id = created_messages[-1][1].split()[2].rstrip(".")
        await app.handle_join_command(user_id=2, chat_id=202, display_name="Bob", table_id=table_id)

        creator_messages = [(text, markup) for chat_id, text, markup in messages if chat_id == 101]
        assert any("ready to start" in text.lower() for text, _markup in creator_messages)
        assert any(markup == [["Start Game"], ["My Table"], ["Cancel Table"], ["Help"]] for _text, markup in creator_messages)

        joiner_messages = [(text, markup) for chat_id, text, markup in messages if chat_id == 202]
        assert any("joined table" in text.lower() or "ready to start" in text.lower() for text, _markup in joiner_messages)
        assert any(markup == [["My Table"], ["Leave Table"], ["Help"]] for _text, markup in joiner_messages)

    asyncio.run(scenario())


def test_remote_telegram_create_flow_rejects_disabled_turn_timer_text() -> None:
    async def scenario() -> None:
        service = make_backend_service()
        app, messages = make_remote_app(make_http_backend_client(service))

        await app.handle_create_table_command(user_id=1, chat_id=101)
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="2")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="0")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="default")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="default")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="default")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="default")
        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="off")

        assert "Enter a turn timer between 1 and 180 seconds." in messages[-1][1]

    asyncio.run(scenario())


def test_remote_telegram_watchers_deliver_turn_prompt_and_completion_updates() -> None:
    async def scenario() -> None:
        service = make_backend_service(llm_outputs=['{"action":"check"}'])
        app, messages = make_remote_app(make_http_backend_client(service))
        await complete_create_flow(app, total_seats="2", llm_seats="0", turn_timeout="15")

        table_id = next(text.split()[2].rstrip(".") for _chat, text, _markup in messages if "Created table" in text)
        await app.handle_join_command(user_id=2, chat_id=202, display_name="Bob", table_id=table_id)
        await app.handle_start_game_command(user_id=1, chat_id=101)
        await asyncio.sleep(0.05)

        assert any("Your move" in text for _chat, text, _markup in messages if _chat == 101)
        assert any(markup == [["Fold"], ["Call"], ["Raise"]] or markup == [["Fold"], ["Check"], ["Bet"]] or markup is not None for _chat, _text, markup in messages if _chat == 101)

        await app.handle_text_message(user_id=1, chat_id=101, display_name="Alice", text="Fold")
        await asyncio.sleep(0.05)

        texts = [text for _chat, text, _markup in messages]
        assert any("Table" in text and "started" in text for text in texts)
        assert any("Table" in text and "completed" in text for text in texts)

        for watcher in app._watchers.values():
            if watcher.task is not None:
                watcher.task.cancel()
        await asyncio.gather(
            *(watcher.task for watcher in app._watchers.values() if watcher.task is not None),
            return_exceptions=True,
        )

    asyncio.run(scenario())


def test_remote_telegram_running_keyboard_switches_between_sit_out_and_sit_in() -> None:
    async def scenario() -> None:
        service = make_backend_service()
        messages: list[tuple[int, str, object | None]] = []

        async def send_message(chat_id: int, text: str, reply_markup: object | None = None) -> None:
            messages.append((chat_id, text, reply_markup))

        app = TelegramApp(
            TelegramAppConfig(
                bot_username="test_bot",
                llm=LLMSettings(model="gpt-test", api_key="test"),
                coach=CoachSettings(enabled=False),
                max_hands_per_table=5,
            ),
            send_message=send_message,
            llm_name_allocator=BotNameAllocator(names=("Nova",), seed=1),
            backend=make_http_backend_client(service),
        )
        await complete_create_flow(app, total_seats="2", llm_seats="0", turn_timeout="30", idle_close="300")

        table_id = next(text.split()[2].rstrip(".") for _chat, text, _markup in messages if "Created table" in text)
        await app.handle_join_command(user_id=2, chat_id=202, display_name="Bob", table_id=table_id)
        await app.handle_start_game_command(user_id=1, chat_id=101)
        await asyncio.sleep(0.05)

        running_keyboard = await app._build_lobby_keyboard(user_id=1, chat_id=101)
        assert running_keyboard == [["My Table"], ["Sit Out"], ["Help"]]

        await app.handle_sit_out_command(user_id=1, chat_id=101)
        await asyncio.sleep(0.05)

        sitting_out_keyboard = await app._build_lobby_keyboard(user_id=1, chat_id=101)
        assert sitting_out_keyboard == [["My Table"], ["Sit In"], ["Help"]]

        for watcher in app._watchers.values():
            if watcher.task is not None:
                watcher.task.cancel()
        await asyncio.gather(
            *(watcher.task for watcher in app._watchers.values() if watcher.task is not None),
            return_exceptions=True,
        )

    asyncio.run(scenario())
