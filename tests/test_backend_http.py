from __future__ import annotations

import asyncio

from meadow.backend.http import HttpBackendClient
from meadow.backend.service import BackendError
from meadow.backend.models import ActorRef, ManagedTableConfig
from meadow.types import TelegramTableState

from support import make_backend_service, make_http_backend_client


async def _wait_for_completion(client: HttpBackendClient, table_id: str, viewer_token: str) -> dict[str, object]:
    snapshot = await client.get_table_snapshot(table_id, viewer_token)
    version = int(snapshot.get("version", 0))
    while snapshot["status"] != TelegramTableState.COMPLETED.value:
        payload = await client.wait_for_table_version(table_id, viewer_token, version, 15_000)
        snapshot = payload["snapshot"]
        version = int(snapshot.get("version", version))
    return snapshot


def test_http_backend_waiting_tables_and_actor_lookup_round_trip_metadata() -> None:
    async def scenario() -> None:
        service = make_backend_service()
        client = make_http_backend_client(service)
        actor = ActorRef(
            transport="telegram",
            external_id="1",
            display_name="Alice",
            metadata={"chat_id": 101, "user_id": 1},
        )
        created = await client.create_table(
            actor,
            ManagedTableConfig(
                total_seats=2,
                llm_seat_count=1,
                small_blind=50,
                big_blind=100,
                turn_timeout_seconds=30,
                idle_close_seconds=300,
                human_transport="telegram",
                human_seat_prefix="tg",
            ),
        )
        waiting = await client.list_waiting_tables()
        assert len(waiting["tables"]) == 1
        waited = await client.wait_for_waiting_tables_version(0, 5)
        assert waited["snapshot"]["tables"][0]["table_id"] == created["table_id"]

        tables = await client.get_actor_tables(actor)
        assert tables["actor"]["metadata"] == {"chat_id": 101, "user_id": 1}
        assert tables["tables"][0]["viewer_token"] == created["viewer_token"]
        assert tables["tables"][0]["is_creator"] is True

    asyncio.run(scenario())


def test_http_backend_requires_turn_timeout_for_human_tables() -> None:
    async def scenario() -> BackendError:
        service = make_backend_service()
        client = make_http_backend_client(service)
        actor = ActorRef(transport="web", external_id="alice", display_name="Alice")
        try:
            await client.create_table(
                actor,
                ManagedTableConfig(
                    total_seats=2,
                    llm_seat_count=1,
                    small_blind=50,
                    big_blind=100,
                    idle_close_seconds=300,
                    human_transport="web",
                    human_seat_prefix="web",
                ),
            )
        except BackendError as exc:
            return exc
        raise AssertionError("Expected BackendError for missing human turn timeout")

    exc = asyncio.run(scenario())
    assert "turn_timeout_seconds is required" in exc.message


def test_http_backend_rejects_human_turn_timeout_above_max() -> None:
    async def scenario() -> BackendError:
        service = make_backend_service()
        client = make_http_backend_client(service)
        actor = ActorRef(transport="web", external_id="alice", display_name="Alice")
        try:
            await client.create_table(
                actor,
                ManagedTableConfig(
                    total_seats=2,
                    llm_seat_count=1,
                    small_blind=50,
                    big_blind=100,
                    turn_timeout_seconds=181,
                    idle_close_seconds=300,
                    human_transport="web",
                    human_seat_prefix="web",
                ),
            )
        except BackendError as exc:
            return exc
        raise AssertionError("Expected BackendError for oversized human turn timeout")

    exc = asyncio.run(scenario())
    assert "between 1 and 180 seconds" in exc.message


def test_http_backend_rejects_idle_close_shorter_than_turn_timeout() -> None:
    async def scenario() -> BackendError:
        service = make_backend_service()
        client = make_http_backend_client(service)
        actor = ActorRef(transport="web", external_id="alice", display_name="Alice")
        try:
            await client.create_table(
                actor,
                ManagedTableConfig(
                    total_seats=2,
                    llm_seat_count=1,
                    small_blind=50,
                    big_blind=100,
                    turn_timeout_seconds=60,
                    idle_close_seconds=30,
                    human_transport="web",
                    human_seat_prefix="web",
                ),
            )
        except BackendError as exc:
            return exc
        raise AssertionError("Expected BackendError for short idle-close timeout")

    exc = asyncio.run(scenario())
    assert "idle_close_seconds must be at least turn_timeout_seconds" in exc.message


def test_http_backend_idle_timeout_completes_running_human_table() -> None:
    async def scenario() -> None:
        service = make_backend_service()
        client = make_http_backend_client(service)
        creator = ActorRef(
            transport="telegram",
            external_id="1",
            display_name="Alice",
            metadata={"chat_id": 101, "user_id": 1},
        )
        created = await client.create_table(
            creator,
            ManagedTableConfig(
                total_seats=2,
                llm_seat_count=1,
                small_blind=50,
                big_blind=100,
                turn_timeout_seconds=1,
                idle_close_seconds=1,
                human_transport="telegram",
                human_seat_prefix="tg",
                max_hands_per_table=5,
            ),
        )

        await client.start_table(creator, created["table_id"], created["viewer_token"])
        completed = await _wait_for_completion(client, created["table_id"], created["viewer_token"])
        assert completed["status"] == TelegramTableState.COMPLETED.value
        assert any(
            event.get("event_type") == "table_completed" and "idle timeout" in event.get("text", "").lower()
            for event in completed["recent_events"]
        )

    asyncio.run(scenario())


def test_http_backend_public_private_snapshots_and_bot_only_tables() -> None:
    async def scenario() -> None:
        service = make_backend_service(llm_outputs=['{"action":"fold"}', '{"action":"check"}'])
        client = make_http_backend_client(service)
        creator = ActorRef(transport="cli", external_id="observer", display_name="CLI observer")
        created = await client.create_table(
            creator,
            ManagedTableConfig(
                total_seats=2,
                llm_seat_count=2,
                small_blind=50,
                big_blind=100,
                max_hands_per_table=1,
                human_transport="cli",
                human_seat_prefix="p",
            ),
        )

        public_waiting = await client.get_table_snapshot(created["table_id"], None)
        assert "participants" not in public_waiting
        assert public_waiting["controls"]["can_join"] is False

        private_waiting = await client.get_table_snapshot(created["table_id"], created["viewer_token"])
        assert private_waiting["viewer_actor"]["external_id"] == "observer"
        assert private_waiting["participants"][0]["metadata"] == {}
        assert private_waiting["controls"]["is_creator"] is True

        await client.start_table(creator, created["table_id"], created["viewer_token"])
        public_running = await client.get_table_snapshot(created["table_id"], None)
        assert "participants" not in public_running
        assert public_running["public_table"] is not None
        assert public_running["player_view"] is None

        completed = await _wait_for_completion(client, created["table_id"], created["viewer_token"])
        assert completed["status"] == TelegramTableState.COMPLETED.value
        assert completed["completed_hands"][0]["hand_number"] == 1

    asyncio.run(scenario())


def test_http_backend_can_sit_out_and_sit_back_in() -> None:
    async def scenario() -> None:
        service = make_backend_service()
        client = make_http_backend_client(service)
        creator = ActorRef(transport="web", external_id="alice", display_name="Alice")
        joiner = ActorRef(transport="web", external_id="bob", display_name="Bob")
        created = await client.create_table(
            creator,
            ManagedTableConfig(
                total_seats=2,
                llm_seat_count=0,
                small_blind=50,
                big_blind=100,
                turn_timeout_seconds=30,
                idle_close_seconds=300,
                human_transport="web",
                human_seat_prefix="web",
                max_hands_per_table=5,
            ),
        )
        joined = await client.join_table(joiner, created["table_id"])
        await client.start_table(creator, created["table_id"], created["viewer_token"])

        await client.sit_out(created["table_id"], created["viewer_token"])

        paused = await client.get_table_snapshot(created["table_id"], created["viewer_token"])
        assert paused["controls"]["is_sitting_out"] is True
        assert any(event.get("event_type") == "seat_sat_out" for event in paused["recent_events"])

        await client.sit_in(created["table_id"], created["viewer_token"])

        resumed = await client.get_table_snapshot(created["table_id"], created["viewer_token"])
        assert resumed["controls"]["is_sitting_out"] is False
        assert joined["viewer_token"] != created["viewer_token"]

    asyncio.run(scenario())
