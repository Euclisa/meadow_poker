from __future__ import annotations

import asyncio
import json
from pathlib import Path
import subprocess

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
    showdown_delay_seconds: float = 5.0,
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
            showdown_delay_seconds=showdown_delay_seconds,
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


def test_web_table_state_exposes_per_street_contributions() -> None:
    app = make_web_app()

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

        start_response = await app.handle_start_table(
            FakeRequest(
                match_info={"table_id": table_id},
                payload={"seat_token": creator_token},
            )
        )
        assert start_response.status == 200
        await asyncio.sleep(0.05)

        preflop_state = await app.handle_table_state(
            FakeRequest(
                match_info={"table_id": table_id},
                query={"seat_token": creator_token},
            )
        )
        preflop_snapshot = decode_json_response(preflop_state)
        preflop_seats = {seat["seat_id"]: seat for seat in preflop_snapshot["public_table"]["seats"]}
        assert preflop_seats["web_1"]["contribution"] == 50
        assert preflop_seats["web_1"]["street_contribution"] == 50
        assert preflop_seats["web_2"]["contribution"] == 100
        assert preflop_seats["web_2"]["street_contribution"] == 100
        assert preflop_snapshot["seat_amount_badges"] == [
            {"seat_id": "web_1", "amount": 50},
            {"seat_id": "web_2", "amount": 100},
        ]

        call_response = await app.handle_submit_action(
            FakeRequest(
                match_info={"table_id": table_id},
                payload={
                    "seat_token": creator_token,
                    "action_type": "call",
                },
            )
        )
        assert call_response.status == 200
        await asyncio.sleep(0.05)

        check_response = await app.handle_submit_action(
            FakeRequest(
                match_info={"table_id": table_id},
                payload={
                    "seat_token": bob_token,
                    "action_type": "check",
                },
            )
        )
        assert check_response.status == 200
        await asyncio.sleep(0.05)

        flop_state = await app.handle_table_state(
            FakeRequest(
                match_info={"table_id": table_id},
                query={"seat_token": bob_token},
            )
        )
        flop_snapshot = decode_json_response(flop_state)
        flop_seats = {seat["seat_id"]: seat for seat in flop_snapshot["public_table"]["seats"]}
        assert flop_seats["web_1"]["contribution"] == 100
        assert flop_seats["web_1"]["street_contribution"] == 0
        assert flop_seats["web_2"]["contribution"] == 100
        assert flop_seats["web_2"]["street_contribution"] == 0
        assert flop_snapshot["seat_amount_badges"] == []

    asyncio.run(scenario())


def test_web_app_showdown_pause_exposes_revealed_cards_and_then_starts_next_hand() -> None:
    app = make_web_app(max_hands=2, showdown_delay_seconds=0.2)

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
        alice_token = created["seat_token"]
        table_id = created["table_id"]

        join_response = await app.handle_join_table(
            FakeRequest(
                match_info={"table_id": table_id},
                payload={"display_name": "Bob"},
            )
        )
        bob_token = decode_json_response(join_response)["seat_token"]

        await app.handle_start_table(
            FakeRequest(
                match_info={"table_id": table_id},
                payload={"seat_token": alice_token},
            )
        )

        for action_type in ("call", "check", "check", "check", "check", "check", "check", "check"):
            acting_token, _snapshot = await _wait_for_any_turn(
                app,
                table_id=table_id,
                seat_tokens=(alice_token, bob_token),
            )
            await _submit_action(app, table_id=table_id, seat_token=acting_token, action_type=action_type)

        showdown_snapshot = await _wait_for_snapshot(
            lambda: _fetch_table_snapshot(app, table_id=table_id, seat_token=alice_token),
            predicate=lambda snapshot: snapshot.get("showdown") is not None,
        )
        assert showdown_snapshot["showdown"]["active"] is True
        assert len(showdown_snapshot["showdown"]["revealed_seats"]) == 2
        assert "winners" not in showdown_snapshot["showdown"]
        assert "resume_at_ms" not in showdown_snapshot["showdown"]
        assert showdown_snapshot["seat_amount_badges"]
        assert showdown_snapshot["public_table"]["hand_number"] == 1

        next_hand_snapshot = await _wait_for_snapshot(
            lambda: _fetch_table_snapshot(app, table_id=table_id, seat_token=alice_token),
            predicate=lambda snapshot: snapshot.get("showdown") is None and snapshot["public_table"]["hand_number"] == 2,
            timeout=1.0,
        )
        assert next_hand_snapshot["public_table"]["hand_number"] == 2
        assert next_hand_snapshot["showdown"] is None

    asyncio.run(scenario())


def test_web_app_folded_hand_does_not_expose_showdown_state() -> None:
    app = make_web_app(max_hands=1, showdown_delay_seconds=0.2)

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
        alice_token = created["seat_token"]
        table_id = created["table_id"]

        join_response = await app.handle_join_table(
            FakeRequest(
                match_info={"table_id": table_id},
                payload={"display_name": "Bob"},
            )
        )
        bob_token = decode_json_response(join_response)["seat_token"]
        assert bob_token

        await app.handle_start_table(
            FakeRequest(
                match_info={"table_id": table_id},
                payload={"seat_token": alice_token},
            )
        )

        await _wait_for_turn(app, table_id=table_id, seat_token=alice_token)
        await _submit_action(app, table_id=table_id, seat_token=alice_token, action_type="fold")
        await asyncio.sleep(0.05)

        snapshot = await _fetch_table_snapshot(app, table_id=table_id, seat_token=alice_token)
        assert snapshot["status"] == "completed"
        assert snapshot["showdown"] is None
        assert snapshot["seat_amount_badges"] == []

    asyncio.run(scenario())


def test_rendered_table_markup_shows_showdown_cards_and_seat_amount_badge() -> None:
    snapshot = {
        "status": "running",
        "table_id": "abcd",
        "message": "Hand complete",
        "config_summary": {
            "web_seats": 2,
            "claimed_web_seats": 2,
            "llm_seats": 0,
            "small_blind": 50,
            "big_blind": 100,
            "starting_stack": 2000,
        },
        "waiting_players": [],
        "controls": {
            "is_joined": True,
            "join_disabled_reason": None,
            "can_start": False,
            "can_cancel": False,
            "can_leave": False,
        },
        "pending_decision": None,
        "seat_amount_badges": [
            {
                "seat_id": "web_1",
                "amount": 400,
            }
        ],
        "recent_events": [],
        "player_view": {
            "seat_id": "web_1",
            "player_name": "Hero",
            "hole_cards": ["2c", "3d"],
            "stack": 1800,
            "contribution": 200,
            "position": "sb",
            "to_call": 0,
        },
        "public_table": {
            "hand_number": 1,
            "phase": "hand_complete",
            "board_cards": ["Ah", "Kd", "7s", "4c", "2h"],
            "pot_total": 400,
            "current_bet": 0,
            "dealer_seat_id": "web_1",
            "acting_seat_id": None,
            "small_blind": 50,
            "big_blind": 100,
            "seats": [
                {
                    "seat_id": "web_1",
                    "name": "Hero",
                    "stack": 1800,
                    "contribution": 200,
                    "street_contribution": 0,
                    "folded": False,
                    "all_in": False,
                    "in_hand": True,
                    "position": "sb",
                    "is_human": True,
                    "is_viewer": True,
                },
                {
                    "seat_id": "web_2",
                    "name": "Villain",
                    "stack": 2200,
                    "contribution": 200,
                    "street_contribution": 0,
                    "folded": False,
                    "all_in": False,
                    "in_hand": True,
                    "position": "bb",
                    "is_human": True,
                    "is_viewer": False,
                },
            ],
        },
        "showdown": {
            "active": True,
            "revealed_seats": [
                {
                    "seat_id": "web_2",
                    "hole_cards": ["As", "Ad"],
                }
            ],
        },
    }

    command = [
        "node",
        "--experimental-default-type=module",
        "-e",
        (
            "import { renderStatusMarkup } from './src/poker_bot/web_app/static/js/table-render.js';"
            "const snapshot = JSON.parse(process.argv[1]);"
            "console.log(renderStatusMarkup(snapshot));"
        ),
        json.dumps(snapshot),
    ]
    result = subprocess.run(command, cwd=Path.cwd(), check=True, text=True, capture_output=True)

    assert 'aria-label="As"' in result.stdout
    assert ">400<" in result.stdout
    assert "Next hand in" not in result.stdout
    assert "Showdown" not in result.stdout


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
    assert "table-seat__bet" in (css_dir / "table.css").read_text()


async def _fetch_table_snapshot(app: WebApp, *, table_id: str, seat_token: str) -> dict:
    response = await app.handle_table_state(
        FakeRequest(
            match_info={"table_id": table_id},
            query={"seat_token": seat_token},
        )
    )
    return decode_json_response(response)


async def _submit_action(app: WebApp, *, table_id: str, seat_token: str, action_type: str) -> None:
    response = await app.handle_submit_action(
        FakeRequest(
            match_info={"table_id": table_id},
            payload={
                "seat_token": seat_token,
                "action_type": action_type,
            },
        )
    )
    assert response.status == 200, decode_json_response(response)
    await asyncio.sleep(0.02)


async def _wait_for_turn(app: WebApp, *, table_id: str, seat_token: str, timeout: float = 0.75) -> dict:
    return await _wait_for_snapshot(
        lambda: _fetch_table_snapshot(app, table_id=table_id, seat_token=seat_token),
        predicate=lambda snapshot: snapshot["pending_decision"] is not None,
        timeout=timeout,
    )


async def _wait_for_any_turn(
    app: WebApp,
    *,
    table_id: str,
    seat_tokens: tuple[str, ...],
    timeout: float = 0.75,
) -> tuple[str, dict]:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        for seat_token in seat_tokens:
            snapshot = await _fetch_table_snapshot(app, table_id=table_id, seat_token=seat_token)
            if snapshot["pending_decision"] is not None:
                return seat_token, snapshot
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("Timed out waiting for any acting seat.")
        await asyncio.sleep(0.01)


async def _wait_for_snapshot(fetch_snapshot, *, predicate, timeout: float = 0.75) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        snapshot = await fetch_snapshot()
        if predicate(snapshot):
            return snapshot
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"Timed out waiting for snapshot: {snapshot}")
        await asyncio.sleep(0.01)
