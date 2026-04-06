from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
import logging
from pathlib import Path
import secrets
from typing import Any

from meadow.backend.models import ActorRef, ManagedTableConfig
from meadow.backend.service import BackendError, LocalBackendClient, LocalTableBackendService
from meadow.config import CoachSettings, LLMSettings
from meadow.naming import BotNameAllocator
from meadow.llm_bot import LLMGameClient

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).with_name("static")
_DEFAULT_BIG_BLIND_PRESETS = (20, 50, 100, 200, 500)
_DEFAULT_STACK_DEPTH_PRESETS = (20, 40, 100, 200)
_DEFAULT_ANTE_PRESETS = (0.0, 0.1, 0.2, 0.5, 1.0)


@dataclass(frozen=True, slots=True)
class WebAppConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    small_blind: int = 50
    big_blind: int = 100
    ante: int = 0
    starting_stack: int = 2_000
    max_players: int = 6
    big_blind_presets: tuple[int, ...] = _DEFAULT_BIG_BLIND_PRESETS
    stack_depth_presets: tuple[int, ...] = _DEFAULT_STACK_DEPTH_PRESETS
    ante_presets: tuple[float, ...] = _DEFAULT_ANTE_PRESETS
    llm: LLMSettings = field(default_factory=LLMSettings)
    coach: CoachSettings = field(default_factory=CoachSettings)
    max_hands_per_table: int | None = None
    showdown_delay_seconds: float = 5.0


class WebApp:
    def __init__(
        self,
        config: WebAppConfig,
        *,
        llm_client_factory: Any | None = None,
        coach_client_factory: Any | None = None,
        llm_name_allocator: BotNameAllocator | None = None,
        backend: Any | None = None,
    ) -> None:
        self.config = config
        self._llm_client_factory = llm_client_factory or self._default_llm_client_factory
        self._coach_client_factory = coach_client_factory or self._default_coach_client_factory
        self._llm_name_allocator = llm_name_allocator
        self.backend = backend or self._build_local_backend()

    def create_http_app(self) -> Any:
        web = self._require_aiohttp()
        app = web.Application()
        app.router.add_get("/", self.handle_lobby_page)
        app.router.add_get("/table/{table_id}", self.handle_table_page)
        app.router.add_get("/table/{table_id}/replay/{hand_number}", self.handle_replay_page)
        app.router.add_get("/api/lobby", self.handle_lobby_state)
        app.router.add_get("/api/lobby/stream", self.handle_lobby_stream)
        app.router.add_post("/api/tables", self.handle_create_table)
        app.router.add_post("/api/tables/{table_id}/join", self.handle_join_table)
        app.router.add_post("/api/tables/{table_id}/start", self.handle_start_table)
        app.router.add_post("/api/tables/{table_id}/leave", self.handle_leave_table)
        app.router.add_post("/api/tables/{table_id}/cancel", self.handle_cancel_table)
        app.router.add_post("/api/tables/{table_id}/action", self.handle_submit_action)
        app.router.add_post("/api/tables/{table_id}/coach", self.handle_request_coach)
        app.router.add_get("/api/tables/{table_id}/state", self.handle_table_state)
        app.router.add_get("/api/tables/{table_id}/replay/{hand_number}", self.handle_replay_state)
        app.router.add_post("/api/tables/{table_id}/replay/{hand_number}/coach", self.handle_request_replay_coach)
        app.router.add_get("/api/tables/{table_id}/stream", self.handle_table_stream)
        app.router.add_static("/static", _STATIC_DIR)
        return app

    async def run(self) -> None:
        web = self._require_aiohttp()
        app = self.create_http_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, host=self.config.host, port=self.config.port)
        await site.start()
        logger.info("Web UI available at http://%s:%s", self.config.host, self.config.port)
        try:
            await asyncio.Event().wait()
        finally:
            await runner.cleanup()

    async def handle_lobby_page(self, request: Any) -> Any:
        del request
        return self._html_response(
            title="Meadow Lobby",
            body_attributes='data-page="lobby"',
            script_name="lobby.js",
        )

    async def handle_table_page(self, request: Any) -> Any:
        table_id = request.match_info["table_id"]
        try:
            await self.backend.get_table_snapshot(table_id, None)
        except BackendError as exc:
            if exc.status == 404:
                raise self._require_aiohttp().HTTPNotFound(text="Table not found.") from exc
        return self._html_response(
            title=f"Table {table_id}",
            body_attributes=f'data-page="table" data-table-id="{table_id}"',
            script_name="table.js",
        )

    async def handle_replay_page(self, request: Any) -> Any:
        table_id = request.match_info["table_id"]
        hand_number = self._parse_int(request.match_info["hand_number"])
        if hand_number is None:
            raise self._require_aiohttp().HTTPNotFound(text="Completed hand not found.")
        try:
            await self.backend.get_replay_snapshot(table_id, None, hand_number, 0)
        except BackendError as exc:
            if exc.status == 404:
                raise self._require_aiohttp().HTTPNotFound(text="Completed hand not found.") from exc
        return self._html_response(
            title=f"Replay Hand {hand_number}",
            body_attributes=(
                f'data-page="replay" data-table-id="{table_id}" '
                f'data-hand-number="{hand_number}"'
            ),
            script_name="replay.js",
        )

    async def handle_lobby_state(self, request: Any) -> Any:
        del request
        return self._json_response(await self._fetch_lobby_snapshot())

    async def handle_lobby_stream(self, request: Any) -> Any:
        return await self._stream_lobby(request)

    async def handle_create_table(self, request: Any) -> Any:
        payload = await self._read_json(request)
        display_name = self._normalize_display_name(payload.get("display_name"))
        total_seats = self._parse_int(payload.get("total_seats"))
        llm_seat_count = self._parse_int(payload.get("llm_seat_count"))
        big_blind = self._parse_int(payload.get("big_blind"))
        stack_depth = self._parse_int(payload.get("stack_depth"))
        ante = self._parse_int(payload.get("ante"))
        if ante is None and payload.get("ante") is None:
            ante = max(0, self.config.ante)
        turn_timeout_seconds = self._parse_int(payload.get("turn_timeout_seconds"))
        self._validate_table_request(
            total_seats=total_seats,
            llm_seat_count=llm_seat_count,
            big_blind=big_blind,
            stack_depth=stack_depth,
            ante=ante,
            turn_timeout_seconds=turn_timeout_seconds,
        )
        assert total_seats is not None
        assert llm_seat_count is not None
        assert big_blind is not None
        assert stack_depth is not None
        assert ante is not None
        actor = self._make_actor(display_name)
        try:
            result = await self.backend.create_table(
                actor,
                ManagedTableConfig(
                    total_seats=total_seats,
                    llm_seat_count=llm_seat_count,
                    small_blind=big_blind // 2,
                    big_blind=big_blind,
                    ante=ante,
                    starting_stack=big_blind * stack_depth,
                    turn_timeout_seconds=turn_timeout_seconds,
                    max_hands_per_table=self.config.max_hands_per_table,
                    max_players=self.config.max_players,
                    human_transport="web",
                    human_seat_prefix="web",
                    stack_depth=stack_depth,
                ),
            )
        except BackendError as exc:
            return self._error_response(exc.message, status=exc.status)
        return self._json_response(
            {
                "table_id": result["table_id"],
                "seat_token": result["viewer_token"],
                "snapshot": self._sanitize_snapshot(result["snapshot"]),
            },
            status=201,
        )

    async def handle_join_table(self, request: Any) -> Any:
        payload = await self._read_json(request)
        display_name = self._normalize_display_name(payload.get("display_name"))
        table_id = request.match_info["table_id"]
        try:
            result = await self.backend.join_table(self._make_actor(display_name), table_id)
        except BackendError as exc:
            return self._error_response(exc.message, status=exc.status)
        return self._json_response(
            {
                "table_id": result["table_id"],
                "seat_token": result["viewer_token"],
                "snapshot": self._sanitize_snapshot(result["snapshot"]),
            }
        )

    async def handle_start_table(self, request: Any) -> Any:
        payload = await self._read_json(request)
        table_id = request.match_info["table_id"]
        seat_token = self._normalize_token(payload.get("seat_token"))
        if seat_token is None:
            return self._error_response("A valid seat token is required.", status=403)
        try:
            actor = await self._actor_for_viewer(table_id, seat_token)
            result = await self.backend.start_table(actor, table_id, seat_token)
        except BackendError as exc:
            return self._error_response(exc.message, status=exc.status)
        return self._json_response({"ok": True, "snapshot": self._sanitize_snapshot(result["snapshot"])})

    async def handle_leave_table(self, request: Any) -> Any:
        payload = await self._read_json(request)
        table_id = request.match_info["table_id"]
        seat_token = self._normalize_token(payload.get("seat_token"))
        if seat_token is None:
            return self._error_response("A valid seat token is required.", status=403)
        try:
            actor = await self._actor_for_viewer(table_id, seat_token)
            result = await self.backend.leave_table(actor, table_id, seat_token)
        except BackendError as exc:
            return self._error_response(exc.message, status=exc.status)
        return self._json_response({"ok": True, "snapshot": self._sanitize_snapshot(result["snapshot"])})

    async def handle_cancel_table(self, request: Any) -> Any:
        payload = await self._read_json(request)
        table_id = request.match_info["table_id"]
        seat_token = self._normalize_token(payload.get("seat_token"))
        if seat_token is None:
            return self._error_response("A valid seat token is required.", status=403)
        try:
            actor = await self._actor_for_viewer(table_id, seat_token)
            result = await self.backend.cancel_table(actor, table_id, seat_token)
        except BackendError as exc:
            return self._error_response(exc.message, status=exc.status)
        return self._json_response({"ok": True, "snapshot": self._sanitize_snapshot(result["snapshot"])})

    async def handle_submit_action(self, request: Any) -> Any:
        payload = await self._read_json(request)
        table_id = request.match_info["table_id"]
        seat_token = self._normalize_token(payload.get("seat_token"))
        action_name = str(payload.get("action_type", "")).strip().lower()
        amount = payload.get("amount")
        if seat_token is None:
            return self._error_response("A valid seat token is required.", status=403)
        try:
            from meadow.types import PlayerAction, ActionType

            action_type = ActionType(action_name)
            result = await self.backend.submit_action(
                table_id,
                seat_token,
                PlayerAction(action_type=action_type, amount=self._parse_int(amount) if amount is not None else None),
            )
        except ValueError:
            return self._error_response("Unknown action type.", status=400)
        except BackendError as exc:
            return self._error_response(exc.message, status=exc.status)
        if not result.get("ok", False):
            snapshot = result.get("snapshot")
            if snapshot is not None:
                result["snapshot"] = self._sanitize_snapshot(snapshot)
            return self._json_response(result, status=400)
        return self._json_response({"ok": True})

    async def handle_request_coach(self, request: Any) -> Any:
        payload = await self._read_json(request)
        table_id = request.match_info["table_id"]
        seat_token = self._normalize_token(payload.get("seat_token"))
        question = str(payload.get("question", "")).strip() or "What should I do in this spot?"
        if seat_token is None:
            return self._error_response("A valid seat token is required.", status=403)
        try:
            result = await self.backend.request_coach(table_id, seat_token, question)
        except BackendError as exc:
            return self._error_response(exc.message, status=exc.status)
        return self._json_response(result)

    async def handle_table_state(self, request: Any) -> Any:
        table_id = request.match_info["table_id"]
        seat_token = self._normalize_token(request.query.get("seat_token"))
        try:
            snapshot = await self.backend.get_table_snapshot(table_id, seat_token)
        except BackendError as exc:
            return self._error_response(exc.message, status=exc.status)
        return self._json_response(self._sanitize_snapshot(snapshot))

    async def handle_replay_state(self, request: Any) -> Any:
        table_id = request.match_info["table_id"]
        hand_number = self._parse_int(request.match_info["hand_number"])
        seat_token = self._normalize_token(request.query.get("seat_token"))
        step_index = self._parse_int(request.query.get("step")) or 0
        if hand_number is None:
            return self._error_response("Invalid hand number.", status=400)
        try:
            snapshot = await self.backend.get_replay_snapshot(table_id, seat_token, hand_number, step_index)
        except BackendError as exc:
            return self._error_response(exc.message, status=exc.status)
        return self._json_response(self._sanitize_snapshot(snapshot))

    async def handle_request_replay_coach(self, request: Any) -> Any:
        payload = await self._read_json(request)
        table_id = request.match_info["table_id"]
        hand_number = self._parse_int(request.match_info["hand_number"])
        seat_token = self._normalize_token(payload.get("seat_token"))
        step_index = self._parse_int(payload.get("step"))
        if hand_number is None:
            return self._error_response("Invalid hand number.", status=400)
        if step_index is None:
            return self._error_response("Replay step is required.", status=400)
        if seat_token is None:
            return self._error_response("A valid seat token is required.", status=403)
        try:
            result = await self.backend.request_replay_coach(table_id, seat_token, hand_number, step_index)
        except BackendError as exc:
            return self._error_response(exc.message, status=exc.status)
        return self._json_response(result)

    async def handle_table_stream(self, request: Any) -> Any:
        table_id = request.match_info["table_id"]
        seat_token = self._normalize_token(request.query.get("seat_token"))
        try:
            await self.backend.get_table_snapshot(table_id, seat_token)
        except BackendError as exc:
            return self._error_response(exc.message, status=exc.status)
        return await self._stream_session(request, table_id=table_id, seat_token=seat_token)

    async def _stream_lobby(self, request: Any) -> Any:
        web = self._require_aiohttp()
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await response.prepare(request)
        try:
            snapshot = await self._fetch_lobby_snapshot()
            await self._write_sse(response, "snapshot", snapshot)
            version = snapshot.get("version", 0)
            while True:
                payload = await self.backend.wait_for_waiting_tables_version(version, 15_000)
                next_snapshot = payload["snapshot"]
                next_version = next_snapshot.get("version", version)
                if next_version == version:
                    await self._write_sse(response, "ping", {"ok": True})
                    continue
                version = next_version
                await self._write_sse(response, "snapshot", next_snapshot)
        except (ConnectionError, RuntimeError):
            return response

    async def _stream_session(self, request: Any, *, table_id: str, seat_token: str | None) -> Any:
        web = self._require_aiohttp()
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await response.prepare(request)
        try:
            snapshot = await self.backend.get_table_snapshot(table_id, seat_token)
            sanitized = self._sanitize_snapshot(snapshot)
            await self._write_sse(response, "snapshot", sanitized)
            version = int(snapshot.get("version", 0))
            while True:
                payload = await self.backend.wait_for_table_version(
                    table_id,
                    seat_token,
                    after_version=version,
                    timeout_ms=15_000,
                )
                next_snapshot = payload["snapshot"]
                next_version = int(next_snapshot.get("version", version))
                if next_version == version:
                    await self._write_sse(response, "ping", {"ok": True})
                    continue
                version = next_version
                await self._write_sse(response, "snapshot", self._sanitize_snapshot(next_snapshot))
        except (BackendError, ConnectionError, RuntimeError):
            return response

    async def _write_sse(self, response: Any, event_name: str, payload: dict[str, Any]) -> None:
        body = f"event: {event_name}\ndata: {json.dumps(payload)}\n\n"
        await response.write(body.encode("utf-8"))

    async def _fetch_lobby_snapshot(self) -> dict[str, Any]:
        snapshot = await self.backend.list_waiting_tables()
        snapshot["defaults"] = self._lobby_defaults()
        return snapshot

    def _lobby_defaults(self) -> dict[str, Any]:
        big_blind_presets = self._big_blind_presets()
        stack_depth_presets = self._stack_depth_presets()
        default_big_blind = self._default_big_blind()
        default_stack_depth = self._default_stack_depth(default_big_blind)
        return {
            "max_players": self.config.max_players,
            "small_blind": default_big_blind // 2,
            "big_blind": default_big_blind,
            "ante": max(0, self.config.ante),
            "starting_stack": default_big_blind * default_stack_depth,
            "stack_depth": default_stack_depth,
            "turn_timeout_seconds": None,
            "big_blind_presets": list(big_blind_presets),
            "stack_depth_presets": list(stack_depth_presets),
            "ante_presets": list(self._ante_presets()),
            "max_hands_per_table": self.config.max_hands_per_table,
        }

    async def _actor_for_viewer(self, table_id: str, viewer_token: str) -> ActorRef:
        snapshot = await self.backend.get_table_snapshot(table_id, viewer_token)
        payload = snapshot.get("viewer_actor")
        if payload is None:
            raise BackendError("A valid seat token is required.", status=403)
        return ActorRef(
            transport=str(payload["transport"]),
            external_id=str(payload["external_id"]),
            display_name=str(payload["display_name"]),
            metadata=dict(payload.get("metadata", {})),
        )

    def _sanitize_snapshot(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        sanitized = json.loads(json.dumps(snapshot))
        sanitized.pop("participants", None)
        sanitized.pop("viewer_actor", None)
        if sanitized.get("config_summary", {}).get("human_transport") == "web":
            summary = sanitized["config_summary"]
            summary["web_seats"] = summary.get("web_seats", summary.get("human_seats"))
            summary["claimed_web_seats"] = summary.get("claimed_web_seats", summary.get("claimed_human_seats"))
        return sanitized

    def _make_actor(self, display_name: str) -> ActorRef:
        return ActorRef(
            transport="web",
            external_id=secrets.token_urlsafe(12),
            display_name=display_name,
        )

    def _validate_table_request(
        self,
        *,
        total_seats: int | None,
        llm_seat_count: int | None,
        big_blind: int | None,
        stack_depth: int | None,
        ante: int | None,
        turn_timeout_seconds: int | None,
    ) -> None:
        if total_seats is None or not 2 <= total_seats <= self.config.max_players:
            raise self._require_aiohttp().HTTPBadRequest(
                text=f"total_seats must be between 2 and {self.config.max_players}."
            )
        if llm_seat_count is None or not 0 <= llm_seat_count < total_seats:
            raise self._require_aiohttp().HTTPBadRequest(
                text=f"llm_seat_count must be between 0 and {total_seats - 1}."
            )
        if big_blind is None or big_blind not in self._big_blind_presets():
            raise self._require_aiohttp().HTTPBadRequest(
                text="big_blind must match one of the supported blind presets."
            )
        if stack_depth is None or stack_depth not in self._stack_depth_presets():
            raise self._require_aiohttp().HTTPBadRequest(
                text="stack_depth must match one of the supported stack presets."
            )
        if ante is None or ante < 0:
            raise self._require_aiohttp().HTTPBadRequest(
                text="ante must be a non-negative integer."
            )
        if turn_timeout_seconds is not None and turn_timeout_seconds <= 0:
            raise self._require_aiohttp().HTTPBadRequest(
                text="turn_timeout_seconds must be positive when set."
            )

    def _build_local_backend(self) -> LocalBackendClient:
        service = LocalTableBackendService(
            llm_client_factory=self._llm_client_factory,
            coach_client_factory=self._coach_client_factory,
            llm_name_allocator=self._llm_name_allocator,
            llm_recent_hand_count=self.config.llm.recent_hand_count,
            llm_thought_logging=self.config.llm.thought_logging,
            coach_enabled=self.config.coach.enabled,
            coach_recent_hand_count=self.config.coach.recent_hand_count,
            showdown_delay_seconds=self.config.showdown_delay_seconds,
        )
        return LocalBackendClient(service)

    def _big_blind_presets(self) -> tuple[int, ...]:
        default_big_blind = self._default_big_blind()
        valid_presets = {preset for preset in self.config.big_blind_presets if preset > 0 and preset % 2 == 0}
        return tuple(sorted({*valid_presets, default_big_blind}))

    def _stack_depth_presets(self) -> tuple[int, ...]:
        default_stack_depth = self._default_stack_depth(self._default_big_blind())
        valid_presets = {preset for preset in self.config.stack_depth_presets if preset > 0}
        return tuple(sorted({*valid_presets, default_stack_depth}))

    def _ante_presets(self) -> tuple[float, ...]:
        valid_presets = {float(preset) for preset in self.config.ante_presets if preset >= 0}
        return tuple(sorted({*valid_presets, 0.0}))

    def _default_big_blind(self) -> int:
        default_big_blind = self.config.big_blind
        if default_big_blind <= 0:
            default_big_blind = 100
        if default_big_blind % 2 != 0:
            default_big_blind += 1
        return default_big_blind

    def _default_stack_depth(self, big_blind: int) -> int:
        return max(1, round(self.config.starting_stack / max(1, big_blind)))

    def _default_llm_client_factory(self) -> LLMGameClient:
        if self.config.llm.model is None or self.config.llm.api_key is None:
            raise RuntimeError("LLM model and API key are required to create LLM seats")
        return LLMGameClient(settings=self.config.llm)

    def _default_coach_client_factory(self) -> LLMGameClient:
        if self.config.coach.model is None or self.config.coach.api_key is None:
            raise RuntimeError("Coach model and API key are required when coach is enabled")
        return LLMGameClient(settings=self.config.coach)

    def _html_response(self, *, title: str, body_attributes: str, script_name: str) -> Any:
        web = self._require_aiohttp()
        html = "\n".join(
            [
                "<!doctype html>",
                "<html lang=\"en\">",
                "<head>",
                "  <meta charset=\"utf-8\">",
                "  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">",
                f"  <title>{title}</title>",
                "  <link rel=\"icon\" type=\"image/svg+xml\" href=\"/static/img/leaf.svg\">",
                "  <link rel=\"stylesheet\" href=\"/static/css/styles.css\">",
                "</head>",
                f"<body {body_attributes}>",
                "  <div id=\"app\"></div>",
                f"  <script type=\"module\" src=\"/static/js/{script_name}\"></script>",
                "</body>",
                "</html>",
            ]
        )
        return web.Response(text=html, content_type="text/html")

    async def _read_json(self, request: Any) -> dict[str, Any]:
        web = self._require_aiohttp()
        try:
            payload = await request.json()
        except Exception as exc:
            raise web.HTTPBadRequest(text="Expected a JSON request body.") from exc
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(text="Expected a JSON object.")
        return payload

    def _json_response(self, payload: dict[str, Any], *, status: int = 200) -> Any:
        web = self._require_aiohttp()
        return web.json_response(payload, status=status)

    def _error_response(self, message: str, *, status: int) -> Any:
        return self._json_response({"ok": False, "error": {"message": message}}, status=status)

    def _normalize_display_name(self, raw: Any) -> str:
        if raw is None:
            raise self._require_aiohttp().HTTPBadRequest(text="display_name is required.")
        display_name = str(raw).strip()
        if not display_name:
            raise self._require_aiohttp().HTTPBadRequest(text="display_name must not be empty.")
        return display_name

    @staticmethod
    def _normalize_token(raw: Any) -> str | None:
        if raw is None:
            return None
        token = str(raw).strip()
        return token or None

    @staticmethod
    def _parse_int(raw: Any) -> int | None:
        if raw is None or raw == "":
            return None
        try:
            return int(raw)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _require_aiohttp() -> Any:
        try:
            from aiohttp import web
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "The aiohttp package is required for web mode. Install meadow[web]."
            ) from exc
        return web
