from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
import logging
from pathlib import Path
from typing import Any

from poker_bot.coach import CoachRequestError, TableCoach
from poker_bot.config import CoachSettings, LLMSettings
from poker_bot.hand_history import render_replay_public_hand_summary
from poker_bot.naming import BotNameAllocator
from poker_bot.orchestrator import GameOrchestrator
from poker_bot.players.llm import LLMGameClient, LLMPlayerAgent
from poker_bot.poker.engine import PokerEngine
from poker_bot.replay import (
    HandReplayBuildError,
    HandReplaySession,
    ReplayAnalysisError,
    build_replay_decision_spot,
)
from poker_bot.table_runner import run_table
from poker_bot.types import ActionType, PlayerAction, PlayerUpdate, PlayerUpdateType, SeatConfig, TableConfig, TelegramTableState
from poker_bot.web_app.player import WebPlayerAgent
from poker_bot.web_app.registry import WebTableRegistry
from poker_bot.web_app.serialization import serialize_lobby, serialize_replay_snapshot, serialize_table_snapshot
from poker_bot.web_app.session import (
    WebShowdownReveal,
    WebShowdownState,
    WebShowdownWinner,
    WebTableCreateRequest,
    WebTableSession,
)

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
        registry: WebTableRegistry | None = None,
    ) -> None:
        self.config = config
        self.registry = registry or WebTableRegistry()
        self._llm_client_factory = llm_client_factory or self._default_llm_client_factory
        self._coach_client_factory = coach_client_factory or self._default_coach_client_factory
        self._llm_name_allocator = llm_name_allocator

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

    async def handle_replay_page(self, request: Any) -> Any:
        table_id = request.match_info["table_id"]
        hand_number = request.match_info["hand_number"]
        session = self.registry.get_table(table_id)
        if session is None:
            raise self._require_aiohttp().HTTPNotFound(text="Table not found.")
        parsed_hand_number = self._parse_int(hand_number)
        if parsed_hand_number is None or session.find_completed_hand(parsed_hand_number) is None:
            raise self._require_aiohttp().HTTPNotFound(text="Completed hand not found.")
        return self._html_response(
            title=f"Replay Hand {hand_number}",
            body_attributes=(
                f'data-page="replay" data-table-id="{table_id}" '
                f'data-hand-number="{hand_number}"'
            ),
            script_name="replay.js",
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

        session, reservation = self.registry.create_waiting_table(
            creator_name=display_name,
            request=WebTableCreateRequest(
                total_seats=total_seats,
                llm_seat_count=llm_seat_count,
                big_blind=big_blind,
                stack_depth=stack_depth,
                ante=ante,
                turn_timeout_seconds=turn_timeout_seconds,
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

    async def handle_request_coach(self, request: Any) -> Any:
        payload = await self._read_json(request)
        table_id = request.match_info["table_id"]
        seat_token = self._normalize_token(payload.get("seat_token"))
        question = str(payload.get("question", "")).strip() or "What should I do in this spot?"

        try:
            session = self._require_table(table_id)
            viewer = self._require_reservation(session, seat_token)
        except KeyError:
            return self._error_response("Table not found.", status=404)
        except PermissionError as exc:
            return self._error_response(str(exc), status=403)

        if session.status != TelegramTableState.RUNNING:
            return self._error_response("Coach tips are only available while the table is running.", status=400)
        if session.coach is None:
            return self._error_response("Coach is not enabled for this table.", status=400)
        agent = session.player_agents.get(viewer.seat_id)
        if not isinstance(agent, WebPlayerAgent) or agent.pending_decision is None:
            return self._error_response("Coach tips are only available on your turn.", status=400)
        if session.orchestrator is None or session.orchestrator.current_hand_record is None:
            return self._error_response("Current hand context is unavailable.", status=400)
        try:
            reply = await session.coach.answer_question(
                table_id=session.table_id,
                seat_id=viewer.seat_id,
                decision=agent.pending_decision,
                current_hand_record=session.orchestrator.current_hand_record,
                question=question,
            )
        except CoachRequestError as exc:
            return self._error_response(str(exc), status=504)
        return self._json_response({"ok": True, "reply": reply})

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

    async def handle_replay_state(self, request: Any) -> Any:
        table_id = request.match_info["table_id"]
        hand_number = self._parse_int(request.match_info["hand_number"])
        seat_token = self._normalize_token(request.query.get("seat_token"))
        step_index = self._parse_int(request.query.get("step")) or 0
        try:
            session = self._require_table(table_id)
            authorized_token = self._authorize_view(session, seat_token)
        except KeyError:
            return self._error_response("Table not found.", status=404)
        except PermissionError as exc:
            return self._error_response(str(exc), status=403)
        if hand_number is None:
            return self._error_response("Invalid hand number.", status=400)

        archive = session.find_completed_hand_archive(hand_number)
        if archive is None:
            return self._error_response("Completed hand not found.", status=404)
        try:
            viewer = session.find_reservation_by_token(authorized_token)
            replay_session = HandReplaySession(
                archive.trace,
                viewer_seat_id=viewer.seat_id if viewer is not None else None,
            )
            frame = replay_session.materialize(step_index)
        except HandReplayBuildError as exc:
            logger.warning("Replay build failed table=%s hand=%s error=%s", table_id, hand_number, exc)
            return self._error_response("Replay could not be built for this hand.", status=500)
        except IndexError:
            return self._error_response("Replay step is out of range.", status=400)
        return self._json_response(
            serialize_replay_snapshot(
                session,
                archive,
                frame,
                seat_token=authorized_token,
            )
        )

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

        try:
            session = self._require_table(table_id)
            viewer = self._require_reservation(session, seat_token)
        except KeyError:
            return self._error_response("Table not found.", status=404)
        except PermissionError as exc:
            return self._error_response(str(exc), status=403)

        if session.coach is None:
            return self._error_response("Coach is not enabled for this table.", status=400)

        archive = session.find_completed_hand_archive(hand_number)
        if archive is None:
            return self._error_response("Completed hand not found.", status=404)

        try:
            spot = build_replay_decision_spot(
                archive.trace,
                step_index=step_index,
                viewer_seat_id=viewer.seat_id,
            )
        except HandReplayBuildError as exc:
            logger.warning("Replay build failed table=%s hand=%s error=%s", table_id, hand_number, exc)
            return self._error_response("Replay could not be built for this hand.", status=500)
        except IndexError:
            return self._error_response("Replay step is out of range.", status=400)
        except ReplayAnalysisError as exc:
            return self._error_response(str(exc), status=400)

        replay_hand_summary = render_replay_public_hand_summary(
            hand_number=archive.record.hand_number,
            events=spot.frame.visible_events,
            start_public_view=archive.record.start_public_view,
            current_public_view=spot.frame.public_table_view,
        )
        try:
            reply = await session.coach.analyze_replay_spot(
                table_id=session.table_id,
                seat_id=viewer.seat_id,
                decision=spot.decision,
                replay_hand_summary=replay_hand_summary,
                next_transition=spot.next_transition,
                replay_hand_number=archive.record.hand_number,
            )
        except CoachRequestError as exc:
            return self._error_response(str(exc), status=504)
        return self._json_response({"ok": True, "reply": reply})

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

        async def handle_turn_timeout(decision: Any, action: PlayerAction) -> None:
            session.add_activity(
                kind="state",
                text=(
                    f"{decision.player_view.player_name} timed out. "
                    f"Auto-{self._format_action_label(action)}."
                ),
            )

        for user in session.claimed_web_users:
            seat_configs.append(SeatConfig(seat_id=user.seat_id, name=user.display_name))
            player_agents[user.seat_id] = WebPlayerAgent(
                seat_id=user.seat_id,
                publish_state=publish_state,
                should_publish_update=self._should_publish_web_update,
            )

        for index in range(1, session.llm_seat_count + 1):
            seat_id = f"llm_{index}"
            seat_configs.append(SeatConfig(seat_id=seat_id, name=self._allocate_llm_name()))
            player_agents[seat_id] = LLMPlayerAgent(
                seat_id=seat_id,
                client=self._llm_client_factory(),
                recent_hand_count=self.config.llm.recent_hand_count,
                thought_logging=self.config.llm.thought_logging,
            )

        engine = PokerEngine.create_table(
            TableConfig(
                small_blind=session.request.small_blind,
                big_blind=session.request.big_blind,
                ante=session.request.ante,
                starting_stack=session.request.starting_stack,
                max_players=session.total_seats,
            ),
            seat_configs,
        )
        orchestrator = GameOrchestrator(
            engine,
            player_agents,
            turn_timeout_seconds=session.request.turn_timeout_seconds,
            on_turn_state_changed=publish_state,
            on_turn_timeout=handle_turn_timeout,
        )
        session.engine = engine
        session.player_agents = player_agents
        session.orchestrator = orchestrator
        session.coach = self._build_table_coach()
        self.registry.mark_running(session)
        session.status_message = f"Table {session.table_id} started with {session.total_seats} seats."
        session.add_activity(kind="state", text=session.status_message)
        await self._broadcast_session(session, include_lobby=True)
        session.orchestrator_task = asyncio.create_task(self._run_session(session))

    async def _run_session(self, session: WebTableSession) -> None:
        assert session.orchestrator is not None

        async def after_hand(result: Any) -> None:
            if result.completed_hand is not None and session.coach is not None:
                await session.coach.record_completed_hand(result.completed_hand)
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
        big_blind_presets = self._big_blind_presets()
        stack_depth_presets = self._stack_depth_presets()
        default_big_blind = self._default_big_blind()
        default_stack_depth = self._default_stack_depth(default_big_blind)
        snapshot["defaults"] = {
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
        return snapshot

    def _snapshot(self, session: WebTableSession, *, seat_token: str | None) -> dict[str, Any]:
        return serialize_table_snapshot(
            session,
            seat_token=seat_token,
            small_blind=session.request.small_blind,
            big_blind=session.request.big_blind,
            ante=session.request.ante,
            starting_stack=session.request.starting_stack,
            max_players=session.total_seats,
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

    @staticmethod
    def _should_publish_web_update(update: PlayerUpdate) -> bool:
        if update.update_type != PlayerUpdateType.HAND_COMPLETED:
            return True
        return not any(event.event_type == "showdown_started" for event in update.events)

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

    @staticmethod
    def _format_action_label(action: PlayerAction) -> str:
        return action.action_type.value if action.amount is None else f"{action.action_type.value} {action.amount}"

    def _default_llm_client_factory(self) -> LLMGameClient:
        if self.config.llm.model is None or self.config.llm.api_key is None:
            raise RuntimeError("LLM model and API key are required to create LLM seats")
        return LLMGameClient(
            settings=self.config.llm,
        )

    def _default_coach_client_factory(self) -> LLMGameClient:
        if self.config.coach.model is None or self.config.coach.api_key is None:
            raise RuntimeError("Coach model and API key are required when coach is enabled")
        return LLMGameClient(settings=self.config.coach)

    def _build_table_coach(self) -> TableCoach | None:
        if not self.config.coach.enabled:
            return None
        return TableCoach(
            self._coach_client_factory(),
            recent_hand_count=self.config.coach.recent_hand_count,
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
                "The aiohttp package is required for web mode. Install poker-bot[web]."
            ) from exc
        return web
