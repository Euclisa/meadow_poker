from __future__ import annotations

import asyncio
import json

import pytest

from meadow.config import CoachSettings, LLMSettings
from meadow.web_app.app import WebApp, WebAppConfig

from support import make_backend_service, make_http_backend_client


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


def make_remote_web_app(backend) -> WebApp:
    return WebApp(
        WebAppConfig(
            llm=LLMSettings(model="gpt-test", api_key="test"),
            coach=CoachSettings(enabled=False),
            max_hands_per_table=1,
        ),
        backend=backend,
    )


async def _wait_for_pending_decision(app: WebApp, table_id: str, alice_token: str, bob_token: str) -> tuple[dict, dict]:
    alice_snapshot: dict | None = None
    bob_snapshot: dict | None = None
    for _ in range(6):
        alice_snapshot = decode_json_response(
            await app.handle_table_state(
                FakeRequest(match_info={"table_id": table_id}, query={"seat_token": alice_token})
            )
        )
        bob_snapshot = decode_json_response(
            await app.handle_table_state(
                FakeRequest(match_info={"table_id": table_id}, query={"seat_token": bob_token})
            )
        )
        if alice_snapshot.get("pending_decision") is not None or bob_snapshot.get("pending_decision") is not None:
            return alice_snapshot, bob_snapshot
        version = max(int(alice_snapshot.get("version", 0)), int(bob_snapshot.get("version", 0)))
        await app.backend.wait_for_table_version(table_id, alice_token, version, 200)
    assert alice_snapshot is not None
    assert bob_snapshot is not None
    return alice_snapshot, bob_snapshot


async def _collect_lobby_stream_snapshots(app: WebApp, *, trigger) -> list[dict]:
    first_snapshot_written = asyncio.Event()
    real_web = app._require_aiohttp()

    class FakeStreamResponse:
        def __init__(self, *, headers: dict[str, str]) -> None:
            self.headers = headers
            self.snapshots: list[dict] = []

        async def prepare(self, request) -> "FakeStreamResponse":
            del request
            return self

        async def write(self, data: bytes) -> None:
            body = data.decode("utf-8")
            if not body.startswith("event: snapshot\n"):
                return
            payload = json.loads(body.split("\ndata: ", 1)[1].strip())
            self.snapshots.append(payload)
            if len(self.snapshots) == 1:
                first_snapshot_written.set()
                return
            raise ConnectionError("stop after second snapshot")

    class FakeWebModule:
        StreamResponse = FakeStreamResponse
        json_response = staticmethod(real_web.json_response)

    app._require_aiohttp = lambda: FakeWebModule
    stream_task = asyncio.create_task(app._stream_lobby(object()))
    await first_snapshot_written.wait()
    await trigger()
    response = await stream_task
    return response.snapshots


def test_remote_web_lobby_sse_updates_after_table_create() -> None:
    async def scenario() -> None:
        service = make_backend_service()
        app = make_remote_web_app(make_http_backend_client(service))

        async def trigger_create() -> None:
            create_response = await app.handle_create_table(
                FakeRequest(
                    payload={
                        "display_name": "Alice",
                        "total_seats": 2,
                        "llm_seat_count": 1,
                        "big_blind": 100,
                        "stack_depth": 20,
                        "turn_timeout_seconds": 30,
                        "idle_close_seconds": 300,
                    }
                )
            )
            assert create_response.status == 201

        snapshots = await _collect_lobby_stream_snapshots(app, trigger=trigger_create)
        assert snapshots[0]["tables"] == []
        assert snapshots[0]["defaults"]["max_players"] == 6
        assert len(snapshots[1]["tables"]) == 1
        assert snapshots[1]["tables"][0]["waiting_players"][0]["display_name"] == "Alice"

    asyncio.run(scenario())


def test_remote_web_table_flow_exposes_safe_public_running_snapshot() -> None:
    async def scenario() -> None:
        service = make_backend_service()
        app = make_remote_web_app(make_http_backend_client(service))

        create_response = await app.handle_create_table(
            FakeRequest(
                payload={
                    "display_name": "Alice",
                    "total_seats": 2,
                    "llm_seat_count": 0,
                    "big_blind": 100,
                    "stack_depth": 20,
                    "turn_timeout_seconds": 30,
                    "idle_close_seconds": 300,
                }
            )
        )
        created = decode_json_response(create_response)
        table_id = created["table_id"]
        alice_token = created["seat_token"]
        assert created["snapshot"]["controls"]["is_creator"] is True

        join_response = await app.handle_join_table(
            FakeRequest(
                match_info={"table_id": table_id},
                payload={"display_name": "Bob"},
            )
        )
        bob_token = decode_json_response(join_response)["seat_token"]

        start_response = await app.handle_start_table(
            FakeRequest(
                match_info={"table_id": table_id},
                payload={"seat_token": alice_token},
            )
        )
        assert start_response.status == 200

        public_state = await app.handle_table_state(
            FakeRequest(match_info={"table_id": table_id})
        )
        public_snapshot = decode_json_response(public_state)
        assert public_snapshot["status"] == "running"
        assert "participants" not in public_snapshot
        assert "viewer_actor" not in public_snapshot
        assert public_snapshot["public_table"] is not None
        assert public_snapshot["player_view"] is None

        alice_snapshot, bob_snapshot = await _wait_for_pending_decision(app, table_id, alice_token, bob_token)
        assert alice_snapshot["controls"]["is_joined"] is True
        assert alice_snapshot["player_view"]["seat_id"] == "web_1"

        acting_snapshot = alice_snapshot if alice_snapshot.get("pending_decision") is not None else bob_snapshot
        acting_token = alice_token if acting_snapshot is alice_snapshot else bob_token
        legal_actions = acting_snapshot["pending_decision"]["legal_actions"]
        action_type = next(
            item["action_type"]
            for item in legal_actions
            if item["action_type"] not in {"bet", "raise"}
        )
        action_response = await app.handle_submit_action(
            FakeRequest(
                match_info={"table_id": table_id},
                payload={"seat_token": acting_token, "action_type": action_type},
            )
        )
        assert action_response.status == 200

        await asyncio.sleep(0.05)
        completed_state = await app.handle_table_state(
            FakeRequest(match_info={"table_id": table_id}, query={"seat_token": bob_token})
        )
        completed_snapshot = decode_json_response(completed_state)
        assert completed_snapshot["status"] == "completed"
        assert completed_snapshot["completed_hands"][0]["hand_number"] == 1

    asyncio.run(scenario())


def test_remote_web_create_table_requires_turn_timeout_for_human_tables() -> None:
    async def scenario() -> None:
        service = make_backend_service()
        app = make_remote_web_app(make_http_backend_client(service))

        with pytest.raises(app._require_aiohttp().HTTPBadRequest) as exc_info:
            await app.handle_create_table(
                FakeRequest(
                    payload={
                        "display_name": "Alice",
                        "total_seats": 2,
                        "llm_seat_count": 1,
                        "big_blind": 100,
                        "stack_depth": 20,
                    }
                )
            )
        assert "turn_timeout_seconds is required" in exc_info.value.text

    asyncio.run(scenario())


def test_remote_web_create_table_rejects_turn_timeout_above_max() -> None:
    async def scenario() -> None:
        service = make_backend_service()
        app = make_remote_web_app(make_http_backend_client(service))

        with pytest.raises(app._require_aiohttp().HTTPBadRequest) as exc_info:
            await app.handle_create_table(
                FakeRequest(
                    payload={
                        "display_name": "Alice",
                        "total_seats": 2,
                        "llm_seat_count": 1,
                        "big_blind": 100,
                        "stack_depth": 20,
                        "turn_timeout_seconds": 181,
                        "idle_close_seconds": 300,
                    }
                )
            )
        assert "between 1 and 180 seconds" in exc_info.value.text

    asyncio.run(scenario())


def test_remote_web_create_table_rejects_idle_close_shorter_than_turn_timeout() -> None:
    async def scenario() -> None:
        service = make_backend_service()
        app = make_remote_web_app(make_http_backend_client(service))

        with pytest.raises(app._require_aiohttp().HTTPBadRequest) as exc_info:
            await app.handle_create_table(
                FakeRequest(
                    payload={
                        "display_name": "Alice",
                        "total_seats": 2,
                        "llm_seat_count": 1,
                        "big_blind": 100,
                        "stack_depth": 20,
                        "turn_timeout_seconds": 60,
                        "idle_close_seconds": 30,
                    }
                )
            )
        assert "idle_close_seconds must be at least turn_timeout_seconds" in exc_info.value.text

    asyncio.run(scenario())
