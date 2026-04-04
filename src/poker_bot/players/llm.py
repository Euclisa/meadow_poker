from __future__ import annotations

import json
import logging
from json import JSONDecodeError
from typing import Any

from poker_bot.players.base import PlayerAgent
from poker_bot.players.rendering import render_events
from poker_bot.types import (
    ActionType,
    DecisionRequest,
    GameEvent,
    PlayerAction,
    PlayerView,
    PublicTableView,
)

logger = logging.getLogger(__name__)


class LLMGameClient:
    def __init__(
        self,
        *,
        model: str,
        api_key: str,
        base_url: str | None = None,
        timeout: float = 30.0,
        max_output_tokens: int | None = None,
        retries: int = 2,
        client: Any | None = None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens
        self.retries = retries
        self._client = client

    async def complete_json(self, prompt: str) -> dict[str, Any]:
        client = self._get_client()
        last_error: Exception | None = None
        logger.debug(
            "LLM request start model=%s base_url=%s timeout=%s max_output_tokens=%s retries=%s prompt=%s",
            self.model,
            self.base_url,
            self.timeout,
            self.max_output_tokens,
            self.retries,
            prompt,
        )
        for attempt in range(self.retries + 1):
            try:
                logger.debug("LLM request attempt=%s model=%s", attempt + 1, self.model)
                request_payload: dict[str, Any] = {
                    "model": self.model,
                    "input": prompt,
                }
                if self.max_output_tokens is not None:
                    request_payload["max_output_tokens"] = self.max_output_tokens
                response = await client.responses.create(**request_payload)
                self._ensure_non_reasoning_output(response)
                raw_text = getattr(response, "output_text", None)
                if not raw_text:
                    raw_text = self._extract_output_text(response)
                logger.debug("LLM raw response model=%s raw_text=%s", self.model, raw_text)
                parsed = self._parse_json_payload(raw_text)
                logger.debug("LLM parsed JSON model=%s payload=%s", self.model, parsed)
                return parsed
            except Exception as exc:  # pragma: no cover - defensive provider wrapper
                last_error = exc
                response_dump = self._safe_debug_dump(locals().get("response"))
                logger.exception(
                    "LLM request failed attempt=%s model=%s response_dump=%s",
                    attempt + 1,
                    self.model,
                    response_dump,
                )
        assert last_error is not None
        raise last_error

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "The openai package is required for LLMGameClient. Install poker-bot[llm]."
            ) from exc

        self._client = AsyncOpenAI(
            api_key=self.api_key,
            base_url=self.base_url,
            timeout=self.timeout,
        )
        return self._client

    @staticmethod
    def _extract_output_text(response: Any) -> str:
        output_items = getattr(response, "output", [])
        text_fragments: list[str] = []
        for item in output_items:
            if getattr(item, "type", None) == "reasoning":
                continue
            for content in getattr(item, "content", []):
                if getattr(content, "type", None) == "reasoning_text":
                    continue
                text = getattr(content, "text", None)
                if text is not None:
                    text_fragments.append(text)
        if not text_fragments:
            raise ValueError("LLM response did not contain non-reasoning output text")
        return "".join(text_fragments)

    @staticmethod
    def _ensure_non_reasoning_output(response: Any) -> None:
        output_items = getattr(response, "output", None)
        if output_items is None:
            return
        for item in output_items:
            if getattr(item, "type", None) == "reasoning":
                continue
            for content in getattr(item, "content", []):
                if getattr(content, "type", None) == "reasoning_text":
                    continue
                if getattr(content, "text", None):
                    return
        raise ValueError("LLM response did not contain non-reasoning output")

    @staticmethod
    def _safe_debug_dump(response: Any) -> str:
        if response is None:
            return "<no response object>"
        model_dump = getattr(response, "model_dump", None)
        if callable(model_dump):
            try:
                return json.dumps(model_dump(), ensure_ascii=False, default=str)
            except Exception:  # pragma: no cover - defensive logging path
                pass
        return repr(response)

    @staticmethod
    def _parse_json_payload(raw_text: str) -> dict[str, Any]:
        try:
            parsed = json.loads(raw_text)
        except JSONDecodeError:
            parsed = LLMGameClient._extract_first_json_object(raw_text)
        if not isinstance(parsed, dict):
            raise ValueError("LLM response JSON must be an object")
        return parsed

    @staticmethod
    def _extract_first_json_object(raw_text: str) -> dict[str, Any]:
        decoder = json.JSONDecoder()
        for index, char in enumerate(raw_text):
            if char != "{":
                continue
            try:
                candidate, _ = decoder.raw_decode(raw_text[index:])
            except JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                return candidate
        raise ValueError("LLM response did not contain a valid JSON object")


class LLMPlayerAgent(PlayerAgent):
    def __init__(self, seat_id: str, client: LLMGameClient, system_prompt: str | None = None) -> None:
        self.seat_id = seat_id
        self.client = client
        self.system_prompt = system_prompt or (
            "You are a poker player. Return exactly one JSON object and nothing else. "
            "Do not include reasoning, explanations, markdown, code fences, or surrounding text."
        )

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        prompt = self._build_prompt(decision)
        logger.debug(
            "LLMPlayerAgent building action seat_id=%s legal_actions=%s decision=%s",
            self.seat_id,
            decision.legal_actions,
            decision,
        )
        payload = await self.client.complete_json(prompt)
        action = self._parse_action(payload)
        logger.debug("LLMPlayerAgent parsed action seat_id=%s action=%s", self.seat_id, action)
        return action

    async def notify_terminal(
        self,
        events: tuple[GameEvent, ...],
        view: PlayerView | PublicTableView,
    ) -> None:
        return None

    async def close(self) -> None:
        return None

    def _build_prompt(self, decision: DecisionRequest) -> str:
        legal_lines = []
        for action in decision.legal_actions:
            if action.min_amount is None:
                legal_lines.append(action.action_type.value)
            else:
                legal_lines.append(
                    f"{action.action_type.value} total={action.min_amount}..{action.max_amount}"
                )

        parts = [
            self.system_prompt,
            f"Seat id: {decision.player_view.seat_id}",
            f"Player name: {decision.player_view.player_name}",
            f"Hole cards: {' '.join(decision.player_view.hole_cards)}",
            f"Board cards: {' '.join(decision.public_table_view.board_cards) or '-'}",
            f"Pot total: {decision.public_table_view.pot_total}",
            f"Current bet: {decision.public_table_view.current_bet}",
            f"To call: {decision.player_view.to_call}",
            f"Stack: {decision.player_view.stack}",
            "Opponents:",
        ]
        for seat in decision.public_table_view.seats:
            if seat.seat_id == decision.player_view.seat_id:
                continue
            parts.append(
                f"- {seat.seat_id} {seat.name}: stack={seat.stack} folded={seat.folded} all_in={seat.all_in}"
            )
        parts.extend(
            [
                "Recent events:",
                render_events(decision.recent_events),
                "Legal actions:",
                *legal_lines,
            ]
        )
        if decision.validation_error is not None:
            parts.append(f"Previous action was invalid: {decision.validation_error.message}")
        parts.append(
            'Output requirements: return exactly one valid JSON object and nothing before or after it.'
        )
        parts.append(
            'Valid examples: {"action":"fold"}, {"action":"check"}, {"action":"call"}, {"action":"bet","amount":400}, {"action":"raise","amount":400}.'
        )
        parts.append(
            'Invalid examples: Here is my move: {"action":"call"}, ```json {"action":"call"} ```, or any explanation text.'
        )
        return "\n".join(parts)

    def _parse_action(self, payload: dict[str, Any]) -> PlayerAction:
        raw_action = payload.get("action")
        if raw_action is None:
            raise ValueError("LLM response must contain an action field")
        try:
            action_type = ActionType(raw_action)
        except ValueError as exc:
            raise ValueError(f"Unsupported LLM action: {raw_action}") from exc

        amount = payload.get("amount")
        if amount is not None:
            amount = int(amount)
        return PlayerAction(action_type=action_type, amount=amount)
