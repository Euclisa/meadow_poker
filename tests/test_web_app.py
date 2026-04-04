from __future__ import annotations

import asyncio
import json
from pathlib import Path

from poker_bot.config import LLMSettings
from poker_bot.naming import BotNameAllocator
from poker_bot.players.llm import LLMGameClient
from poker_bot.web_app.app import WebApp, WebAppConfig


class FakeResponsesAPI:
    def __init__(self, outputs: list[str]) -> None:
        self.outputs = list(outputs)

    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_output_tokens: int | None = None,
        extra_body: dict | None = None,
    ) -> object:
        output = self.outputs.pop(0) if self.outputs else '{"action":"check"}'
        message = type("Message", (), {"content": output})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


class FakeOpenAIClient:
    def __init__(self, outputs: list[str]) -> None:
        self.chat = type("Chat", (), {"completions": FakeResponsesAPI(outputs)})()


def make_web_app(
    *,
    max_hands: int | None = None,
    llm_outputs: list[str] | None = None,
) -> WebApp:
    llm_outputs = llm_outputs or ['{"action":"check"}'] * 20

    def make_llm_client() -> LLMGameClient:
        return LLMGameClient(
            settings=LLMSettings(
                model="gpt-test",
                api_key="test",
            ),
            client=FakeOpenAIClient(list(llm_outputs)),
        )

    return WebApp(
        WebAppConfig(
            llm=LLMSettings(
                model="gpt-test",
                api_key="test",
            ),
            max_hands_per_table=max_hands,
        ),
        llm_client_factory=make_llm_client,
        llm_name_allocator=BotNameAllocator(names=("Nova",), seed=1),
    )


class FakeRequest:
    def __init__(
        self,
        *,
        match_info: dict[str, str] | None = None,
        query: dict[str, str] | None = None,
        payload: dict | None = None,
    ) -> None:
        self.match_info = match_info or {}
        self.query = query or {}
        self._payload = payload or {}

    async def json(self) -> dict:
        return self._payload


def decode_json_response(response) -> dict:
    return json.loads(response.text)


def test_web_app_lobby_stream_and_html_shell() -> None:
    app = make_web_app()

    async def scenario() -> None:
        lobby_page = await app.handle_lobby_page(FakeRequest())
        assert lobby_page.status == 200
        assert 'src="/static/js/lobby.js"' in lobby_page.text
        assert 'id="app"' in lobby_page.text

        initial_lobby = app._lobby_snapshot()
        assert initial_lobby["tables"] == []
        assert initial_lobby["defaults"]["max_players"] == 6

        queue = app.registry.subscribe_lobby()
        try:
            create_response = await app.handle_create_table(
                FakeRequest(
                    payload={
                        "display_name": "Alice",
                        "total_seats": 3,
                        "llm_seat_count": 1,
                    }
                )
            )
            assert create_response.status == 201
            created = decode_json_response(create_response)
            assert created["snapshot"]["controls"]["is_creator"] is True

            await asyncio.wait_for(queue.get(), timeout=0.2)
            updated_lobby = app._lobby_snapshot()
            assert len(updated_lobby["tables"]) == 1
            assert updated_lobby["tables"][0]["table_id"] == created["table_id"]

            table_page = await app.handle_table_page(
                FakeRequest(match_info={"table_id": created["table_id"]})
            )
            assert table_page.status == 200
            assert f'data-table-id="{created["table_id"]}"' in table_page.text
        finally:
            app.registry.unsubscribe_lobby(queue)

    asyncio.run(scenario())


def test_web_app_two_player_table_invalid_action_then_fold_completion() -> None:
    app = make_web_app(max_hands=1)

    async def scenario() -> None:
        create_response = await app.handle_create_table(
            FakeRequest(
                payload={
                    "display_name": "Alice",
                    "total_seats": 2,
                    "llm_seat_count": 0,
                }
            )
        )
        created = decode_json_response(create_response)
        creator_token = created["seat_token"]
        table_id = created["table_id"]

        join_response = await app.handle_join_table(
            FakeRequest(
                match_info={"table_id": table_id},
                payload={"display_name": "Bob"},
            )
        )
        joined = decode_json_response(join_response)
        bob_token = joined["seat_token"]
        assert joined["snapshot"]["controls"]["is_joined"] is True

        session = app.registry.get_table(table_id)
        assert session is not None
        queue = session.subscribe()
        try:
            start_response = await app.handle_start_table(
                FakeRequest(
                    match_info={"table_id": table_id},
                    payload={"seat_token": creator_token},
                )
            )
            assert start_response.status == 200
            await asyncio.wait_for(queue.get(), timeout=0.2)
            await asyncio.sleep(0.05)

            creator_state = await app.handle_table_state(
                FakeRequest(
                    match_info={"table_id": table_id},
                    query={"seat_token": creator_token},
                )
            )
            creator_snapshot = decode_json_response(creator_state)
            assert creator_snapshot["controls"]["is_joined"] is True
            assert creator_snapshot["pending_decision"] is not None
            assert creator_snapshot["player_view"]["seat_id"] == "web_1"

            invalid_response = await app.handle_submit_action(
                FakeRequest(
                    match_info={"table_id": table_id},
                    payload={
                        "seat_token": creator_token,
                        "action_type": "bet",
                        "amount": 300,
                    },
                )
            )
            assert invalid_response.status == 400
            invalid_payload = decode_json_response(invalid_response)
            assert invalid_payload["error"]["code"] == "illegal_action"
            assert invalid_payload["snapshot"]["pending_decision"] is not None

            bob_state = await app.handle_table_state(
                FakeRequest(
                    match_info={"table_id": table_id},
                    query={"seat_token": bob_token},
                )
            )
            bob_snapshot = decode_json_response(bob_state)
            assert bob_snapshot["controls"]["is_joined"] is True
            assert bob_snapshot["player_view"]["seat_id"] == "web_2"

            fold_response = await app.handle_submit_action(
                FakeRequest(
                    match_info={"table_id": table_id},
                    payload={
                        "seat_token": creator_token,
                        "action_type": "fold",
                    },
                )
            )
            assert fold_response.status == 200

            await asyncio.wait_for(queue.get(), timeout=0.2)
            await asyncio.sleep(0.05)
            completed_state = await app.handle_table_state(
                FakeRequest(
                    match_info={"table_id": table_id},
                    query={"seat_token": creator_token},
                )
            )
            completed_snapshot = decode_json_response(completed_state)
            assert completed_snapshot["status"] == "completed"
            assert any("Table" in event["text"] for event in completed_snapshot["recent_events"])

            public_running = await app.handle_table_state(
                FakeRequest(match_info={"table_id": table_id})
            )
            assert public_running.status == 403
        finally:
            session.unsubscribe(queue)

    asyncio.run(scenario())


def test_web_app_llm_table_can_complete_and_preserve_token_rejoin() -> None:
    app = make_web_app(max_hands=1, llm_outputs=['{"action":"check"}'] * 20)

    async def scenario() -> None:
        create_response = await app.handle_create_table(
            FakeRequest(
                payload={
                    "display_name": "Alice",
                    "total_seats": 2,
                    "llm_seat_count": 1,
                }
            )
        )
        created = decode_json_response(create_response)
        seat_token = created["seat_token"]
        table_id = created["table_id"]

        waiting_state = await app.handle_table_state(
            FakeRequest(match_info={"table_id": table_id})
        )
        waiting_snapshot = decode_json_response(waiting_state)
        assert waiting_snapshot["controls"]["can_join"] is False
        assert waiting_snapshot["controls"]["join_disabled_reason"] == "All web seats are already claimed."

        start_response = await app.handle_start_table(
            FakeRequest(
                match_info={"table_id": table_id},
                payload={"seat_token": seat_token},
            )
        )
        assert start_response.status == 200
        await asyncio.sleep(0.05)

        rejoined_state = await app.handle_table_state(
            FakeRequest(
                match_info={"table_id": table_id},
                query={"seat_token": seat_token},
            )
        )
        running_snapshot = decode_json_response(rejoined_state)
        assert running_snapshot["controls"]["seat_token_valid"] is True
        assert any(seat["name"] == "Nova_bot" for seat in running_snapshot["public_table"]["seats"])

        action_response = await app.handle_submit_action(
            FakeRequest(
                match_info={"table_id": table_id},
                payload={
                    "seat_token": seat_token,
                    "action_type": "fold",
                },
            )
        )
        assert action_response.status == 200
        await asyncio.sleep(0.05)

        completed_state = await app.handle_table_state(
            FakeRequest(
                match_info={"table_id": table_id},
                query={"seat_token": seat_token},
            )
        )
        completed_snapshot = decode_json_response(completed_state)
        assert completed_snapshot["status"] == "completed"
        assert completed_snapshot["controls"]["seat_token_valid"] is True
        assert completed_snapshot["player_view"]["player_name"] == "Alice"

    asyncio.run(scenario())


def test_frontend_static_files_exist_and_include_core_hooks() -> None:
    js_dir = Path("src/poker_bot/web_app/static/js")
    css_dir = Path("src/poker_bot/web_app/static/css")

    assert "export function renderCard" in (js_dir / "shared.js").read_text()
    assert "renderLobbyMarkup" in (js_dir / "lobby.js").read_text()
    assert "renderTableMarkup" in (js_dir / "table.js").read_text()
    styles = (css_dir / "styles.css").read_text()
    assert "@import" in styles
    assert ".playing-card" in (css_dir / "cards.css").read_text()
    assert "@media (max-width: 640px)" in (css_dir / "responsive.css").read_text()
