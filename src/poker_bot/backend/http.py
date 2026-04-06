from __future__ import annotations

import json
from typing import Any

from poker_bot.backend.models import ActorRef, ManagedTableConfig
from poker_bot.backend.serialization import actor_to_dict, jsonable, managed_table_config_to_dict, player_action_from_dict, player_action_to_dict
from poker_bot.backend.service import BackendError
from poker_bot.types import PlayerAction


class HttpBackendClient:
    def __init__(self, gateway_url: str, *, session: Any | None = None) -> None:
        self._gateway_url = gateway_url.rstrip("/")
        self._session = session

    async def list_waiting_tables(self) -> dict[str, Any]:
        return await self._request("GET", "/tables")

    async def wait_for_waiting_tables_version(
        self,
        after_version: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        return await self._request(
            "GET",
            "/tables/wait",
            params={"after_version": str(after_version), "timeout_ms": str(timeout_ms)},
        )

    async def create_table(self, actor: ActorRef, table_config: ManagedTableConfig) -> dict[str, Any]:
        return await self._request(
            "POST",
            "/tables",
            json={
                "actor": actor_to_dict(actor),
                "table_config": managed_table_config_to_dict(table_config),
            },
        )

    async def join_table(self, actor: ActorRef, table_id: str) -> dict[str, Any]:
        return await self._request("POST", f"/tables/{table_id}/join", json={"actor": actor_to_dict(actor)})

    async def start_table(self, actor: ActorRef, table_id: str, viewer_token: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/tables/{table_id}/start",
            json={"actor": actor_to_dict(actor), "viewer_token": viewer_token},
        )

    async def leave_table(self, actor: ActorRef, table_id: str, viewer_token: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/tables/{table_id}/leave",
            json={"actor": actor_to_dict(actor), "viewer_token": viewer_token},
        )

    async def cancel_table(self, actor: ActorRef, table_id: str, viewer_token: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/tables/{table_id}/cancel",
            json={"actor": actor_to_dict(actor), "viewer_token": viewer_token},
        )

    async def get_table_snapshot(self, table_id: str, viewer_token: str | None) -> dict[str, Any]:
        params = {}
        if viewer_token is not None:
            params["viewer_token"] = viewer_token
        return await self._request("GET", f"/tables/{table_id}", params=params)

    async def wait_for_table_version(
        self,
        table_id: str,
        viewer_token: str | None,
        after_version: int,
        timeout_ms: int,
    ) -> dict[str, Any]:
        params = {
            "after_version": str(after_version),
            "timeout_ms": str(timeout_ms),
        }
        if viewer_token is not None:
            params["viewer_token"] = viewer_token
        return await self._request("GET", f"/tables/{table_id}/wait", params=params)

    async def submit_action(self, table_id: str, viewer_token: str, action: PlayerAction) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/tables/{table_id}/actions",
            json={"viewer_token": viewer_token, "action": player_action_to_dict(action)},
        )

    async def request_coach(self, table_id: str, viewer_token: str, question: str) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/tables/{table_id}/coach",
            json={"viewer_token": viewer_token, "question": question},
        )

    async def get_replay_snapshot(
        self,
        table_id: str,
        viewer_token: str | None,
        hand_number: int,
        step_index: int,
    ) -> dict[str, Any]:
        params = {"step": str(step_index)}
        if viewer_token is not None:
            params["viewer_token"] = viewer_token
        return await self._request("GET", f"/tables/{table_id}/replays/{hand_number}", params=params)

    async def request_replay_coach(
        self,
        table_id: str,
        viewer_token: str,
        hand_number: int,
        step_index: int,
    ) -> dict[str, Any]:
        return await self._request(
            "POST",
            f"/tables/{table_id}/replays/{hand_number}/coach",
            json={"viewer_token": viewer_token, "step": step_index},
        )

    async def get_actor_tables(self, actor: ActorRef) -> dict[str, Any]:
        return await self._request(
            "GET",
            f"/actors/{actor.transport}/{actor.external_id}/tables",
            params={
                "display_name": actor.display_name,
                "metadata": json.dumps(jsonable(actor.metadata)),
            },
        )

    async def _request(self, method: str, path: str, *, params: dict[str, Any] | None = None, json: dict[str, Any] | None = None) -> dict[str, Any]:
        session = self._session
        if session is None:
            try:
                from aiohttp import ClientSession
            except ImportError as exc:  # pragma: no cover - optional dependency
                raise RuntimeError("The aiohttp package is required for remote backend mode.") from exc
            async with ClientSession() as temp_session:
                return await self._perform_request(temp_session, method, path, params=params, json=json)
        return await self._perform_request(session, method, path, params=params, json=json)

    async def _perform_request(self, session: Any, method: str, path: str, *, params: dict[str, Any] | None = None, json: dict[str, Any] | None = None) -> dict[str, Any]:
        async with session.request(method, f"{self._gateway_url}{path}", params=params, json=json) as response:
            payload = await response.json()
            if response.status >= 400:
                error = payload.get("error", {})
                raise BackendError(
                    str(error.get("message", "Backend request failed.")),
                    status=response.status,
                    code=str(error.get("code", "backend_error")),
                )
            return payload


def create_backend_http_app(service: Any) -> Any:
    try:
        from aiohttp import web
    except ImportError as exc:  # pragma: no cover - optional dependency
        raise RuntimeError("The aiohttp package is required for backend server mode.") from exc

    app = web.Application()

    async def handle_list_tables(request: Any) -> Any:
        return web.json_response(await service.list_waiting_tables())

    async def handle_create_table(request: Any) -> Any:
        payload = await request.json()
        actor = _actor_from_payload(payload["actor"])
        config = _config_from_payload(payload["table_config"])
        return web.json_response(await service.create_table(actor, config), status=201)

    async def handle_wait_tables(request: Any) -> Any:
        return web.json_response(
            await service.wait_for_waiting_tables_version(
                int(request.query.get("after_version", "0")),
                int(request.query.get("timeout_ms", "15000")),
            )
        )

    async def handle_join_table(request: Any) -> Any:
        payload = await request.json()
        actor = _actor_from_payload(payload["actor"])
        return web.json_response(await service.join_table(actor, request.match_info["table_id"]))

    async def handle_start_table(request: Any) -> Any:
        payload = await request.json()
        actor = _actor_from_payload(payload["actor"])
        return web.json_response(await service.start_table(actor, request.match_info["table_id"], str(payload["viewer_token"])))

    async def handle_leave_table(request: Any) -> Any:
        payload = await request.json()
        actor = _actor_from_payload(payload["actor"])
        return web.json_response(await service.leave_table(actor, request.match_info["table_id"], str(payload["viewer_token"])))

    async def handle_cancel_table(request: Any) -> Any:
        payload = await request.json()
        actor = _actor_from_payload(payload["actor"])
        return web.json_response(await service.cancel_table(actor, request.match_info["table_id"], str(payload["viewer_token"])))

    async def handle_get_table(request: Any) -> Any:
        return web.json_response(await service.get_table_snapshot(request.match_info["table_id"], request.query.get("viewer_token")))

    async def handle_wait_table(request: Any) -> Any:
        return web.json_response(
            await service.wait_for_table_version(
                request.match_info["table_id"],
                request.query.get("viewer_token"),
                int(request.query.get("after_version", "0")),
                int(request.query.get("timeout_ms", "15000")),
            )
        )

    async def handle_submit_action(request: Any) -> Any:
        payload = await request.json()
        return web.json_response(await service.submit_action(request.match_info["table_id"], str(payload["viewer_token"]), player_action_from_dict(payload["action"])))

    async def handle_request_coach(request: Any) -> Any:
        payload = await request.json()
        return web.json_response(await service.request_coach(request.match_info["table_id"], str(payload["viewer_token"]), str(payload.get("question", ""))))

    async def handle_get_replay(request: Any) -> Any:
        return web.json_response(
            await service.get_replay_snapshot(
                request.match_info["table_id"],
                request.query.get("viewer_token"),
                int(request.match_info["hand_number"]),
                int(request.query.get("step", "0")),
            )
        )

    async def handle_replay_coach(request: Any) -> Any:
        payload = await request.json()
        return web.json_response(
            await service.request_replay_coach(
                request.match_info["table_id"],
                str(payload["viewer_token"]),
                int(request.match_info["hand_number"]),
                int(payload["step"]),
            )
        )

    async def handle_actor_tables(request: Any) -> Any:
        metadata = request.query.get("metadata")
        actor = ActorRef(
            transport=request.match_info["transport"],
            external_id=request.match_info["external_id"],
            display_name=request.query.get("display_name", request.match_info["external_id"]),
            metadata={} if metadata in {None, ""} else json.loads(metadata),
        )
        return web.json_response(await service.get_actor_tables(actor))

    @web.middleware
    async def backend_error_middleware(request: Any, handler: Any) -> Any:
        try:
            return await handler(request)
        except BackendError as exc:
            return web.json_response(
                {"error": {"code": exc.code, "message": exc.message}},
                status=exc.status,
            )

    app.middlewares.append(backend_error_middleware)
    app.router.add_get("/tables", handle_list_tables)
    app.router.add_get("/tables/wait", handle_wait_tables)
    app.router.add_post("/tables", handle_create_table)
    app.router.add_post("/tables/{table_id}/join", handle_join_table)
    app.router.add_post("/tables/{table_id}/start", handle_start_table)
    app.router.add_post("/tables/{table_id}/leave", handle_leave_table)
    app.router.add_post("/tables/{table_id}/cancel", handle_cancel_table)
    app.router.add_get("/tables/{table_id}", handle_get_table)
    app.router.add_get("/tables/{table_id}/wait", handle_wait_table)
    app.router.add_post("/tables/{table_id}/actions", handle_submit_action)
    app.router.add_post("/tables/{table_id}/coach", handle_request_coach)
    app.router.add_get("/tables/{table_id}/replays/{hand_number}", handle_get_replay)
    app.router.add_post("/tables/{table_id}/replays/{hand_number}/coach", handle_replay_coach)
    app.router.add_get("/actors/{transport}/{external_id}/tables", handle_actor_tables)
    return app


def _actor_from_payload(payload: dict[str, Any]) -> ActorRef:
    return ActorRef(
        transport=str(payload["transport"]),
        external_id=str(payload["external_id"]),
        display_name=str(payload["display_name"]),
        metadata=dict(payload.get("metadata", {})),
    )


def _config_from_payload(payload: dict[str, Any]) -> ManagedTableConfig:
    return ManagedTableConfig(**payload)
