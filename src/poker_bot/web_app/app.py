from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any

from poker_bot.config import LLMSettings
from poker_bot.naming import BotNameAllocator
from poker_bot.orchestrator import GameOrchestrator
from poker_bot.players.llm import LLMGameClient, LLMPlayerAgent
from poker_bot.poker.engine import PokerEngine
from poker_bot.table_runner import run_table
from poker_bot.types import ActionType, PlayerAction, SeatConfig, TableConfig, TelegramTableState
from poker_bot.web_app.player import WebPlayerAgent
from poker_bot.web_app.registry import WebTableRegistry
from poker_bot.web_app.serialization import serialize_lobby, serialize_table_snapshot
from poker_bot.web_app.session import (
    WebShowdownReveal,
    WebShowdownState,
    WebShowdownWinner,
    WebTableCreateRequest,
    WebTableSession,
)

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).with_name("static")


@dataclass(frozen=True, slots=True)
class WebAppConfig:
    host: str = "127.0.0.1"
    port: int = 8080
    small_blind: int = 50
    big_blind: int = 100
    starting_stack: int = 2_000
    max_players: int = 6
    llm: LLMSettings = field(default_factory=LLMSettings)
    max_hands_per_table: int | None = None
    showdown_delay_seconds: float = 5.0


class WebApp:
    def __init__(
        self,
        config: WebAppConfig,
        *,
        llm_client_factory: Any | None = None,
        llm_name_allocator: BotNameAllocator | None = None,
        registry: WebTableRegistry | None = None,
    ) -> None:
        self.config = config
        self.registry = registry or WebTableRegistry()
        self._llm_client_factory = llm_client_factory or self._default_llm_client_factory
        self._llm_name_allocator = llm_name_allocator

    def create_http_app(self) -> Any:
        web = self._require_aiohttp()
        app = web.Application()
        app.router.add_get("/", self.handle_lobby_page)
        app.router.add_get("/table/{table_id}", self.handle_table_page)
        app.router.add_get("/api/lobby", self.handle_lobby_state)
        app.router.add_get("/api/lobby/stream", self.handle_lobby_stream)
        app.router.add_post("/api/tables", self.handle_create_table)
        app.router.add_post("/api/tables/{table_id}/join", self.handle_join_table)
        app.router.add_post("/api/tables/{table_id}/start", self.handle_start_table)
        app.router.add_post("/api/tables/{table_id}/leave", self.handle_leave_table)
        app.router.add_post("/api/tables/{table_id}/cancel", self.handle_cancel_table)
        app.router.add_post("/api/tables/{table_id}/action", self.handle_submit_action)
        app.router.add_get("/api/tables/{table_id}/state", self.handle_table_state)
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
        return self._html_response(
            title="Poker Bot Lobby",
            body_attributes='data-page="lobby"',
            script_name="lobby.js",
        )

    async def handle_table_page(self, request: Any) -> Any:
        table_id = request.match_info["table_id"]
        session = self.registry.get_table(table_id)
        if session is None:
            raise self._require_aiohttp().HTTPNotFound(text="Table not found.")
        return self._html_response(
            title=f"Table {table_id}",
            body_attributes=f'data-page="table" data-table-id="{table_id}"',
            script_name="table.js",
        )

    async def handle_lobby_state(self, request: Any) -> Any:
        return self._json_response(self._lobby_snapshot())

    async def handle_lobby_stream(self, request: Any) -> Any:
        return await self._stream_lobby(request)

    async def handle_create_table(self, request: Any) -> Any:
        payload = await self._read_json(request)
        display_name = self._normalize_display_name(payload.get("display_name"))
        total_seats = self._parse_int(payload.get("total_seats"))
        llm_seat_count = self._parse_int(payload.get("llm_seat_count"))
        self._validate_table_request(total_seats=total_seats, llm_seat_count=llm_seat_count)

        session, reservation = self.registry.create_waiting_table(
            creator_name=display_name,
            request=WebTableCreateRequest(
                total_seats=total_seats,
                llm_seat_count=llm_seat_count,
            ),
        )
        self._sync_waiting_message(session)
        await self._broadcast_session(session, include_lobby=True)
        return self._json_response(
            {
                "table_id": session.table_id,
                "seat_token": reservation.seat_token,
                "snapshot": self._snapshot(session, seat_token=reservation.seat_token),
            },
            status=201,
        )

    async def handle_join_table(self, request: Any) -> Any:
        payload = await self._read_json(request)
        display_name = self._normalize_display_name(payload.get("display_name"))
        table_id = request.match_info["table_id"]
        try:
            session, reservation = self.registry.join_table(
                table_id=table_id,
                display_name=display_name,
            )
        except KeyError as exc:
            return self._error_response(str(exc), status=404)
        except ValueError as exc:
            return self._error_response(str(exc), status=400)

        self._sync_waiting_message(session)
        await self._broadcast_session(session, include_lobby=True)
        return self._json_response(
            {
                "table_id": session.table_id,
                "seat_token": reservation.seat_token,
                "snapshot": self._snapshot(session, seat_token=reservation.seat_token),
            }
        )

    async def handle_start_table(self, request: Any) -> Any:
        payload = await self._read_json(request)
        table_id = request.match_info["table_id"]
        seat_token = self._normalize_token(payload.get("seat_token"))
        try:
            session = self._require_table(table_id)
            viewer = self._require_reservation(session, seat_token)
        except KeyError:
            return self._error_response("Table not found.", status=404)
        except PermissionError as exc:
            return self._error_response(str(exc), status=403)

        if not session.is_creator_token(seat_token):
            return self._error_response("Only the creator can start the table.", status=403)
        if session.status != TelegramTableState.WAITING:
            return self._error_response("Only waiting tables can be started.", status=400)
        if not session.is_full():
            return self._error_response("All web seats must be claimed before starting.", status=400)

        await self._start_table(session)
        return self._json_response(
            {
                "ok": True,
                "snapshot": self._snapshot(session, seat_token=viewer.seat_token),
            }
        )

    async def handle_leave_table(self, request: Any) -> Any:
        payload = await self._read_json(request)
        table_id = request.match_info["table_id"]
        seat_token = self._normalize_token(payload.get("seat_token"))
        try:
            session = self.registry.leave_waiting_table(table_id=table_id, seat_token=seat_token)
        except KeyError:
            return self._error_response("Table not found.", status=404)
        except ValueError as exc:
            return self._error_response(str(exc), status=400)

        self._sync_waiting_message(session)
        await self._broadcast_session(session, include_lobby=True)
        return self._json_response(
            {
                "ok": True,
                "snapshot": self._snapshot(session, seat_token=None),
            }
        )

    async def handle_cancel_table(self, request: Any) -> Any:
        payload = await self._read_json(request)
        table_id = request.match_info["table_id"]
        seat_token = self._normalize_token(payload.get("seat_token"))
        try:
            session = self._require_table(table_id)
        except KeyError:
            return self._error_response("Table not found.", status=404)
        if not session.is_creator_token(seat_token):
            return self._error_response("Only the creator can cancel the table.", status=403)
        if session.status != TelegramTableState.WAITING:
            return self._error_response("Only waiting tables can be cancelled.", status=400)

        cancelled = self.registry.cancel_table(table_id)
        cancelled.status_message = f"Table {cancelled.table_id} was cancelled."
        cancelled.add_activity(kind="state", text=cancelled.status_message)
        await self._broadcast_session(cancelled, include_lobby=True)
        return self._json_response(
            {
                "ok": True,
                "snapshot": self._snapshot(cancelled, seat_token=seat_token),
            }
        )

    async def handle_submit_action(self, request: Any) -> Any:
        payload = await self._read_json(request)
        table_id = request.match_info["table_id"]
        seat_token = self._normalize_token(payload.get("seat_token"))
        action_name = str(payload.get("action_type", "")).strip().lower()
        amount = payload.get("amount")

        try:
            session = self._require_table(table_id)
            viewer = self._require_reservation(session, seat_token)
        except KeyError:
            return self._error_response("Table not found.", status=404)
        except PermissionError as exc:
            return self._error_response(str(exc), status=403)

        if session.status != TelegramTableState.RUNNING:
            return self._error_response("Actions are only accepted while the table is running.", status=400)

        agent = session.player_agents.get(viewer.seat_id)
        if not isinstance(agent, WebPlayerAgent):
            return self._error_response("This seat is not controlled from the web UI.", status=400)

        try:
            action_type = ActionType(action_name)
        except ValueError:
            return self._error_response("Unknown action type.", status=400)

        parsed_amount = self._parse_int(amount) if amount is not None else None
        error = agent.submit_action(PlayerAction(action_type=action_type, amount=parsed_amount))
        if error is not None:
            return self._json_response(
                {
                    "ok": False,
                    "error": {
                        "code": error.code,
                        "message": error.message,
                    },
                    "snapshot": self._snapshot(session, seat_token=seat_token),
                },
                status=400,
            )
        return self._json_response({"ok": True})

    async def handle_table_state(self, request: Any) -> Any:
        table_id = request.match_info["table_id"]
        seat_token = self._normalize_token(request.query.get("seat_token"))
        try:
            session = self._require_table(table_id)
            authorized_token = self._authorize_view(session, seat_token)
        except KeyError:
            return self._error_response("Table not found.", status=404)
        except PermissionError as exc:
            return self._error_response(str(exc), status=403)

        return self._json_response(self._snapshot(session, seat_token=authorized_token))

    async def handle_table_stream(self, request: Any) -> Any:
        table_id = request.match_info["table_id"]
        seat_token = self._normalize_token(request.query.get("seat_token"))
        try:
            session = self._require_table(table_id)
            authorized_token = self._authorize_view(session, seat_token)
        except KeyError:
            return self._error_response("Table not found.", status=404)
        except PermissionError as exc:
            return self._error_response(str(exc), status=403)
        return await self._stream_session(request, session=session, seat_token=authorized_token)

    async def _start_table(self, session: WebTableSession) -> None:
        seat_configs: list[SeatConfig] = []
        player_agents: dict[str, Any] = {}

        async def publish_state() -> None:
            await self._broadcast_session(session, include_lobby=False)

        for user in session.claimed_web_users:
            seat_configs.append(SeatConfig(seat_id=user.seat_id, name=user.display_name))
            player_agents[user.seat_id] = WebPlayerAgent(
                seat_id=user.seat_id,
                publish_state=publish_state,
            )

        for index in range(1, session.llm_seat_count + 1):
            seat_id = f"llm_{index}"
            seat_configs.append(SeatConfig(seat_id=seat_id, name=self._allocate_llm_name()))
            player_agents[seat_id] = LLMPlayerAgent(
                seat_id=seat_id,
                client=self._llm_client_factory(),
                recent_hand_count=self.config.llm.recent_hand_count,
                log_thoughts=self.config.llm.log_thoughts,
            )

        engine = PokerEngine.create_table(
            TableConfig(
                small_blind=self.config.small_blind,
                big_blind=self.config.big_blind,
                starting_stack=self.config.starting_stack,
                max_players=self.config.max_players,
            ),
            seat_configs,
        )
        orchestrator = GameOrchestrator(engine, player_agents)
        session.engine = engine
        session.player_agents = player_agents
        session.orchestrator = orchestrator
        self.registry.mark_running(session)
        session.status_message = f"Table {session.table_id} started with {session.total_seats} seats."
        session.add_activity(kind="state", text=session.status_message)
        await self._broadcast_session(session, include_lobby=True)
        session.orchestrator_task = asyncio.create_task(self._run_session(session))

    async def _run_session(self, session: WebTableSession) -> None:
        assert session.orchestrator is not None

        async def after_hand(result: Any) -> None:
            if not result.ended_in_showdown:
                return
            session.showdown_state = self._build_showdown_state(result)
            await self._broadcast_session(session, include_lobby=False)
            await asyncio.sleep(self.config.showdown_delay_seconds)
            session.showdown_state = None
            await self._broadcast_session(session, include_lobby=False)

        try:
            await run_table(
                session.orchestrator,
                max_hands=self.config.max_hands_per_table,
                close_agents=True,
                after_hand=after_hand,
            )
        finally:
            logger.info("Web table %s completed", session.table_id)
            self.registry.mark_completed(session)
            session.status_message = f"Table {session.table_id} has completed."
            session.add_activity(kind="state", text=session.status_message)
            await self._broadcast_session(session, include_lobby=True)

    async def _broadcast_session(self, session: WebTableSession, *, include_lobby: bool) -> None:
        session.notify_watchers()
        if include_lobby:
            self.registry.notify_lobby_watchers()

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
        queue = self.registry.subscribe_lobby()
        try:
            await self._write_sse(response, "snapshot", self._lobby_snapshot())
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    await self._write_sse(response, "ping", {"ok": True})
                    continue
                await self._write_sse(response, "snapshot", self._lobby_snapshot())
        except (ConnectionError, RuntimeError):
            return response
        finally:
            self.registry.unsubscribe_lobby(queue)

    async def _stream_session(self, request: Any, *, session: WebTableSession, seat_token: str | None) -> Any:
        web = self._require_aiohttp()
        response = web.StreamResponse(
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
            }
        )
        await response.prepare(request)
        queue = session.subscribe()
        try:
            await self._write_sse(response, "snapshot", self._snapshot(session, seat_token=seat_token))
            while True:
                try:
                    await asyncio.wait_for(queue.get(), timeout=15.0)
                except TimeoutError:
                    await self._write_sse(response, "ping", {"ok": True})
                    continue
                await self._write_sse(response, "snapshot", self._snapshot(session, seat_token=seat_token))
        except (ConnectionError, RuntimeError):
            return response
        finally:
            session.unsubscribe(queue)

    async def _write_sse(self, response: Any, event_name: str, payload: dict[str, Any]) -> None:
        body = f"event: {event_name}\ndata: {json.dumps(payload)}\n\n"
        await response.write(body.encode("utf-8"))

    def _lobby_snapshot(self) -> dict[str, Any]:
        snapshot = serialize_lobby(self.registry)
        snapshot["defaults"] = {
            "max_players": self.config.max_players,
            "small_blind": self.config.small_blind,
            "big_blind": self.config.big_blind,
            "starting_stack": self.config.starting_stack,
            "max_hands_per_table": self.config.max_hands_per_table,
        }
        return snapshot

    def _snapshot(self, session: WebTableSession, *, seat_token: str | None) -> dict[str, Any]:
        return serialize_table_snapshot(
            session,
            seat_token=seat_token,
            small_blind=self.config.small_blind,
            big_blind=self.config.big_blind,
            starting_stack=self.config.starting_stack,
            max_players=self.config.max_players,
            max_hands_per_table=self.config.max_hands_per_table,
        )

    def _build_showdown_state(self, result: Any) -> WebShowdownState:
        revealed_seats = tuple(
            WebShowdownReveal(
                seat_id=event.payload["seat_id"],
                hole_cards=tuple(event.payload["hole_cards"]),
            )
            for event in result.events
            if event.event_type == "showdown_revealed"
        )
        winners = tuple(
            WebShowdownWinner(
                seat_id=event.payload["seat_id"],
                amount=event.payload["amount"],
            )
            for event in result.events
            if event.event_type == "pot_awarded"
        )
        return WebShowdownState(
            revealed_seats=revealed_seats,
            winners=winners,
        )

    def _authorize_view(self, session: WebTableSession, seat_token: str | None) -> str | None:
        if session.find_reservation_by_token(seat_token) is not None:
            return seat_token
        if session.status == TelegramTableState.WAITING:
            return None
        raise PermissionError("A valid seat token is required to view this table.")

    def _require_table(self, table_id: str) -> WebTableSession:
        table = self.registry.get_table(table_id)
        if table is None:
            raise KeyError(table_id)
        return table

    def _require_reservation(self, session: WebTableSession, seat_token: str | None) -> Any:
        reservation = session.find_reservation_by_token(seat_token)
        if reservation is None:
            raise PermissionError("A valid seat token is required.")
        return reservation

    def _sync_waiting_message(self, session: WebTableSession) -> None:
        if session.status != TelegramTableState.WAITING:
            return
        if session.is_full():
            session.status_message = f"Table {session.table_id} is ready to start."
            return
        session.status_message = (
            f"Waiting for {session.open_web_seat_count()} more player"
            f"{'' if session.open_web_seat_count() == 1 else 's'}."
        )

    def _validate_table_request(self, *, total_seats: int | None, llm_seat_count: int | None) -> None:
        if total_seats is None or not 2 <= total_seats <= self.config.max_players:
            raise self._require_aiohttp().HTTPBadRequest(
                text=f"total_seats must be between 2 and {self.config.max_players}."
            )
        if llm_seat_count is None or not 0 <= llm_seat_count < total_seats:
            raise self._require_aiohttp().HTTPBadRequest(
                text=f"llm_seat_count must be between 0 and {total_seats - 1}."
            )

    def _default_llm_client_factory(self) -> LLMGameClient:
        if self.config.llm.model is None or self.config.llm.api_key is None:
            raise RuntimeError("LLM model and API key are required to create LLM seats")
        return LLMGameClient(
            settings=self.config.llm,
        )

    def _allocate_llm_name(self) -> str:
        if self._llm_name_allocator is None:
            self._llm_name_allocator = BotNameAllocator()
        return self._llm_name_allocator.allocate()

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
                "The aiohttp package is required for web mode. Install poker-bot[web]."
            ) from exc
        return web
