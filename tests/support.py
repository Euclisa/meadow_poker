from __future__ import annotations

import asyncio
import json
from urllib.parse import urlparse

from poker_bot.backend.models import ActorRef, ManagedTableConfig
from poker_bot.backend.serialization import player_action_from_dict
from poker_bot.backend.service import BackendError
from poker_bot.backend.service import LocalTableBackendService
from poker_bot.config import CoachSettings, LLMSettings
from poker_bot.naming import BotNameAllocator
from poker_bot.players.llm import LLMGameClient


class FakeResponsesAPI:
    def __init__(self, outputs: list[str], *, delay: float = 0.0) -> None:
        self.outputs = list(outputs)
        self.delay = delay
        self.messages_list: list[list[dict[str, str]]] = []

    async def create(
        self,
        *,
        model: str,
        messages: list[dict[str, str]],
        max_output_tokens: int | None = None,
        extra_body: dict | None = None,
    ) -> object:
        del model, max_output_tokens, extra_body
        self.messages_list.append(messages)
        if self.delay:
            await asyncio.sleep(self.delay)
        output = self.outputs.pop(0) if self.outputs else '{"action":"check"}'
        message = type("Message", (), {"content": output})()
        choice = type("Choice", (), {"message": message})()
        return type("Response", (), {"choices": [choice]})()


class FakeOpenAIClient:
    def __init__(self, outputs: list[str], *, delay: float = 0.0) -> None:
        self.chat = type("Chat", (), {"completions": FakeResponsesAPI(outputs, delay=delay)})()


def make_llm_client_factory(
    outputs: list[str] | None = None,
    *,
    delay: float = 0.0,
    settings: LLMSettings | None = None,
) -> callable:
    configured_settings = settings or LLMSettings(model="gpt-test", api_key="test")
    configured_outputs = outputs or ['{"action":"check"}'] * 32

    def make_client() -> LLMGameClient:
        return LLMGameClient(
            settings=configured_settings,
            client=FakeOpenAIClient(list(configured_outputs), delay=delay),
        )

    return make_client


def make_coach_client_factory(
    outputs: list[str] | None = None,
    *,
    delay: float = 0.0,
) -> callable:
    configured_outputs = outputs or ["Coach reply"]

    def make_client() -> LLMGameClient:
        return LLMGameClient(
            settings=CoachSettings(
                enabled=True,
                model="gpt-coach",
                api_key="coach-test",
                timeout=0.2,
            ),
            client=FakeOpenAIClient(list(configured_outputs), delay=delay),
        )

    return make_client


def make_backend_service(
    *,
    llm_outputs: list[str] | None = None,
    coach_outputs: list[str] | None = None,
    coach_delay: float = 0.0,
    showdown_delay_seconds: float = 0.0,
) -> LocalTableBackendService:
    return LocalTableBackendService(
        llm_client_factory=make_llm_client_factory(llm_outputs),
        coach_client_factory=make_coach_client_factory(coach_outputs, delay=coach_delay) if coach_outputs is not None else None,
        llm_name_allocator=BotNameAllocator(names=("Nova", "Milo", "Rhea"), seed=1),
        llm_recent_hand_count=5,
        coach_enabled=coach_outputs is not None,
        coach_recent_hand_count=5,
        showdown_delay_seconds=showdown_delay_seconds,
    )


class _InMemoryResponse:
    def __init__(self, status: int, payload: dict) -> None:
        self.status = status
        self._payload = payload

    async def __aenter__(self) -> "_InMemoryResponse":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def json(self) -> dict:
        return self._payload


class InMemoryBackendSession:
    def __init__(self, service: LocalTableBackendService) -> None:
        self._service = service

    def request(self, method: str, url: str, *, params: dict | None = None, json: dict | None = None):
        return _InMemoryResponseTask(self._service, method, url, params=params or {}, json_body=json or {})


class _InMemoryResponseTask:
    def __init__(
        self,
        service: LocalTableBackendService,
        method: str,
        url: str,
        *,
        params: dict,
        json_body: dict,
    ) -> None:
        self._service = service
        self._method = method
        self._url = url
        self._params = params
        self._json_body = json_body

    async def __aenter__(self) -> _InMemoryResponse:
        try:
            payload = await self._dispatch()
            return _InMemoryResponse(200, payload)
        except BackendError as exc:
            return _InMemoryResponse(exc.status, {"error": {"code": exc.code, "message": exc.message}})

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def _dispatch(self) -> dict:
        path = urlparse(self._url).path
        segments = [segment for segment in path.split("/") if segment]
        if self._method == "GET" and segments == ["tables"]:
            return await self._service.list_waiting_tables()
        if self._method == "GET" and segments == ["tables", "wait"]:
            return await self._service.wait_for_waiting_tables_version(
                int(self._params.get("after_version", "0")),
                int(self._params.get("timeout_ms", "15000")),
            )
        if self._method == "POST" and segments == ["tables"]:
            return await self._service.create_table(
                _actor_from_payload(self._json_body["actor"]),
                ManagedTableConfig(**self._json_body["table_config"]),
            )
        if len(segments) >= 2 and segments[0] == "tables":
            table_id = segments[1]
            if self._method == "POST" and segments[2:] == ["join"]:
                return await self._service.join_table(_actor_from_payload(self._json_body["actor"]), table_id)
            if self._method == "POST" and segments[2:] == ["start"]:
                return await self._service.start_table(_actor_from_payload(self._json_body["actor"]), table_id, str(self._json_body["viewer_token"]))
            if self._method == "POST" and segments[2:] == ["leave"]:
                return await self._service.leave_table(_actor_from_payload(self._json_body["actor"]), table_id, str(self._json_body["viewer_token"]))
            if self._method == "POST" and segments[2:] == ["cancel"]:
                return await self._service.cancel_table(_actor_from_payload(self._json_body["actor"]), table_id, str(self._json_body["viewer_token"]))
            if self._method == "GET" and len(segments) == 2:
                return await self._service.get_table_snapshot(table_id, self._params.get("viewer_token"))
            if self._method == "GET" and segments[2:] == ["wait"]:
                return await self._service.wait_for_table_version(
                    table_id,
                    self._params.get("viewer_token"),
                    int(self._params.get("after_version", "0")),
                    int(self._params.get("timeout_ms", "15000")),
                )
            if self._method == "POST" and segments[2:] == ["actions"]:
                return await self._service.submit_action(table_id, str(self._json_body["viewer_token"]), player_action_from_dict(self._json_body["action"]))
            if self._method == "POST" and segments[2:] == ["coach"]:
                return await self._service.request_coach(table_id, str(self._json_body["viewer_token"]), str(self._json_body.get("question", "")))
            if len(segments) >= 4 and segments[2] == "replays":
                hand_number = int(segments[3])
                if self._method == "GET" and len(segments) == 4:
                    return await self._service.get_replay_snapshot(table_id, self._params.get("viewer_token"), hand_number, int(self._params.get("step", "0")))
                if self._method == "POST" and segments[4:] == ["coach"]:
                    return await self._service.request_replay_coach(table_id, str(self._json_body["viewer_token"]), hand_number, int(self._json_body["step"]))
        if self._method == "GET" and len(segments) == 4 and segments[0] == "actors" and segments[3] == "tables":
            actor = ActorRef(
                transport=segments[1],
                external_id=segments[2],
                display_name=str(self._params.get("display_name", segments[2])),
                metadata={} if self._params.get("metadata") in {None, ""} else json.loads(str(self._params["metadata"])),
            )
            return await self._service.get_actor_tables(actor)
        raise AssertionError(f"Unhandled in-memory backend request: {self._method} {path}")


def make_http_backend_client(service: LocalTableBackendService):
    from poker_bot.backend.http import HttpBackendClient

    return HttpBackendClient("http://backend.test", session=InMemoryBackendSession(service))


def _actor_from_payload(payload: dict) -> ActorRef:
    return ActorRef(
        transport=str(payload["transport"]),
        external_id=str(payload["external_id"]),
        display_name=str(payload["display_name"]),
        metadata=dict(payload.get("metadata", {})),
    )
