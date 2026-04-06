from __future__ import annotations

import asyncio

from poker_bot.backend.http import HttpBackendClient
from poker_bot.backend.models import ActorRef, ManagedTableConfig
from poker_bot.types import TelegramTableState

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
