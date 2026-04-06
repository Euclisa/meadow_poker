from __future__ import annotations

import json
import logging
from json import JSONDecodeError
from dataclasses import dataclass
from typing import Any

from meadow.config import CoachSettings, LLMSettings, ThoughtLoggingMode
from meadow.hand_history import render_private_completed_hand_summary
from meadow.player_agent import PlayerAgent
from meadow.rendering.core import render_player_update
from meadow.types import (
    ActionType,
    DecisionRequest,
    HandRecord,
    PlayerAction,
    PlayerUpdate,
    PlayerUpdateType,
    PlayerView,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class LLMCompletionResult:
    payload: dict[str, Any]
    raw_text: str


class LLMGameClient:
    def __init__(
        self,
        *,
        settings: LLMSettings | CoachSettings,
        retries: int = 2,
        client: Any | None = None,
    ) -> None:
        self.settings = settings
        self.retries = retries
        self._client = client

    async def complete_json(self, messages: list[dict[str, str]]) -> LLMCompletionResult:
        raw_text = await self.complete_text(messages)
        parsed = self._parse_json_payload(raw_text)
        logger.debug("LLM parsed JSON model=%s payload=%s", self.settings.model, parsed)
        return LLMCompletionResult(payload=parsed, raw_text=raw_text)

    async def complete_text(self, messages: list[dict[str, str]]) -> str:
        client = self._get_client()
        last_error: Exception | None = None
        logger.debug(
            "LLM request start model=%s base_url=%s timeout=%s max_output_tokens=%s retries=%s messages=%s",
            self.settings.model,
            self.settings.base_url,
            self.settings.timeout,
            self.settings.max_output_tokens,
            self.retries,
            messages,
        )
        for attempt in range(self.retries + 1):
            try:
                logger.debug("LLM request attempt=%s model=%s", attempt + 1, self.settings.model)
                request_payload: dict[str, Any] = {
                    "model": self.settings.model,
                    "messages": messages,
                }
                if self.settings.max_output_tokens is not None:
                    request_payload["max_output_tokens"] = self.settings.max_output_tokens
                extra_body = self.settings.to_extra_body()
                if extra_body:
                    request_payload["extra_body"] = extra_body
                response = await client.chat.completions.create(**request_payload)
                raw_text = self._extract_chat_completion_text(response)
                logger.debug("LLM raw response model=%s raw_text=%s", self.settings.model, raw_text)
                return raw_text
            except Exception as exc:  # pragma: no cover - defensive provider wrapper
                last_error = exc
                response_dump = self._safe_debug_dump(locals().get("response"))
                logger.warning(
                    "LLM request failed attempt=%s/%s model=%s",
                    attempt + 1,
                    self.retries + 1,
                    self.settings.model,
                )
                logger.debug("LLM failure details response_dump=%s", response_dump)
        assert last_error is not None
        raise last_error

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        try:
            from openai import AsyncOpenAI
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "The openai package is required for LLMGameClient. Install meadow[llm]."
            ) from exc

        self._client = AsyncOpenAI(
            api_key=self.settings.api_key,
            base_url=self.settings.base_url,
            timeout=self.settings.timeout,
        )
        return self._client

    @staticmethod
    def _extract_chat_completion_text(response: Any) -> str:
        choices = getattr(response, "choices", None) or []
        if not choices:
            raise ValueError("LLM response did not contain any completion choices")
        message = getattr(choices[0], "message", None)
        if message is None:
            raise ValueError("LLM response did not contain a completion message")
        content = getattr(message, "content", None)
        if isinstance(content, str) and content.strip():
            return content
        if isinstance(content, list):
            text_fragments: list[str] = []
            for item in content:
                text = getattr(item, "text", None)
                if isinstance(text, str) and text:
                    text_fragments.append(text)
            if text_fragments:
                return "".join(text_fragments)
        raise ValueError("LLM response did not contain text output")

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
    _REFLECTION_PROMPT = (
        "You are reviewing completed poker hands to update your private strategic notes. "
        "Write only the updated note text and nothing else. "
        "Use the summaries to record opponent tendencies, table dynamics, showdown patterns, "
        "adjustments to make, and uncertainties to keep watching."
    )

    def __init__(
        self,
        seat_id: str,
        client: LLMGameClient,
        system_prompt: str | None = None,
        recent_hand_count: int = 5,
        thought_logging: ThoughtLoggingMode = ThoughtLoggingMode.OFF,
    ) -> None:
        self.seat_id = seat_id
        self.client = client
        self.system_prompt = system_prompt or (
            "You are a poker player. Return exactly one JSON object and nothing else. "
            "Do not include reasoning, explanations, markdown, code fences, or surrounding text."
        )
        self.recent_hand_count = max(0, recent_hand_count)
        self.thought_logging = thought_logging
        self._current_hand_number: int | None = None
        self._history: list[dict[str, str]] = []
        self._buffered_updates: list[PlayerUpdate] = []
        self._pending_hand_summaries: list[str] = []
        self._reflection_note: str | None = None

    @property
    def keeps_table_alive(self) -> bool:
        return False

    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        self._reset_history_if_needed(decision)
        prompt = self._build_prompt(decision)
        messages = self._build_messages(prompt)
        logger.debug(
            "LLMPlayerAgent building action seat_id=%s legal_actions=%s hand_number=%s decision=%s history=%s",
            self.seat_id,
            decision.legal_actions,
            decision.public_table_view.hand_number,
            decision,
            self._history,
        )
        completion = await self.client.complete_json(messages)
        self._history.append({"role": "user", "content": prompt})
        self._history.append({"role": "assistant", "content": completion.raw_text})
        self._buffered_updates.clear()
        action = self._parse_action(completion.payload)
        logger.debug("LLMPlayerAgent parsed action seat_id=%s action=%s", self.seat_id, action)
        return action

    async def notify_update(self, update: PlayerUpdate) -> None:
        if update.events:
            self._buffered_updates.append(update)
        if update.update_type in {PlayerUpdateType.HAND_COMPLETED, PlayerUpdateType.TABLE_COMPLETED}:
            self._current_hand_number = None
            self._history.clear()
            self._buffered_updates.clear()
        return None

    async def on_hand_completed(self, record: HandRecord, player_view: PlayerView) -> None:
        if self.recent_hand_count <= 0:
            return
        summary = render_private_completed_hand_summary(record, player_view)
        if self.thought_logging.logs_hand_summaries:
            logger.info("LLM thought seat_id=%s type=hand_summary\n%s", self.seat_id, summary)
        self._pending_hand_summaries.append(summary)
        if len(self._pending_hand_summaries) >= self.recent_hand_count:
            await self._update_reflection_note()

    async def close(self) -> None:
        self._current_hand_number = None
        self._history.clear()
        self._buffered_updates.clear()
        self._pending_hand_summaries.clear()
        self._reflection_note = None
        return None

    def _build_messages(self, prompt: str) -> list[dict[str, str]]:
        messages = [{"role": "developer", "content": self.system_prompt}]
        if self._reflection_note is not None:
            messages.append(
                {
                    "role": "developer",
                    "content": (
                        "These are your current observations from prior hands. "
                        "Use them as your working note, but update your play using the live hand state.\n\n"
                        f"{self._reflection_note}"
                    ),
                }
            )
        messages.extend(self._history)
        messages.append({"role": "user", "content": prompt})
        return messages

    def _reset_history_if_needed(self, decision: DecisionRequest) -> None:
        hand_number = decision.public_table_view.hand_number
        if self._current_hand_number == hand_number:
            return
        self._current_hand_number = hand_number
        self._history.clear()
        self._buffered_updates.clear()

    def _build_prompt(self, decision: DecisionRequest) -> str:
        summary_parts = [
            "Current decision:",
            render_player_update(
                PlayerUpdate(
                    update_type=PlayerUpdateType.TURN_STARTED,
                    events=(),
                    public_table_view=decision.public_table_view,
                    player_view=decision.player_view,
                    acting_seat_id=decision.acting_seat_id,
                    is_your_turn=True,
                )
            ),
            "",
            render_player_update(
                PlayerUpdate(
                    update_type=PlayerUpdateType.TURN_STARTED,
                    events=(),
                    public_table_view=decision.public_table_view,
                    player_view=decision.player_view,
                    acting_seat_id=decision.acting_seat_id,
                    is_your_turn=True,
                ),
                compact=True,
            ),
            "",
            f"Hole cards: {' '.join(decision.player_view.hole_cards)}",
            f"Legal actions: {', '.join(self._format_legal_action(action) for action in decision.legal_actions)}",
        ]
        history_text = self._render_buffered_updates()
        if history_text:
            summary_parts.extend(["", "Recent events:", history_text])
        if decision.validation_error is not None:
            summary_parts.extend(["", f"Previous action was invalid: {decision.validation_error.message}"])
        return "\n".join(summary_parts)

    def _render_buffered_updates(self) -> str:
        return "\n\n".join(render_player_update(update, compact=True) for update in self._buffered_updates)

    @staticmethod
    def _format_legal_action(action) -> str:
        if action.action_type in {ActionType.BET, ActionType.RAISE}:
            return f"{action.action_type.value} {action.min_amount}-{action.max_amount}"
        return action.action_type.value

    @staticmethod
    def _parse_action(payload: dict[str, Any]) -> PlayerAction:
        raw_action = str(payload.get("action", "")).strip().lower()
        try:
            action_type = ActionType(raw_action)
        except ValueError as exc:
            raise ValueError(f"Unsupported action '{raw_action}'") from exc
        amount = payload.get("amount")
        if amount is None:
            return PlayerAction(action_type=action_type)
        try:
            parsed_amount = int(amount)
        except (TypeError, ValueError) as exc:
            raise ValueError("Action amount must be an integer") from exc
        return PlayerAction(action_type=action_type, amount=parsed_amount)

    async def _update_reflection_note(self) -> None:
        messages = [{"role": "developer", "content": self._REFLECTION_PROMPT}]
        prompt_parts = [
            "Completed private hand summaries to synthesize:",
            "\n\n".join(self._pending_hand_summaries),
        ]
        if self._reflection_note is not None:
            prompt_parts.extend(
                [
                    "",
                    "Your current note:",
                    self._reflection_note,
                    "",
                    "Revise the note using the new completed hands.",
                ]
            )
        else:
            prompt_parts.extend(["", "Write the first note based on these hands."])
        messages.append({"role": "user", "content": "\n".join(prompt_parts)})
        updated_note = (await self.client.complete_text(messages)).strip()
        self._pending_hand_summaries.clear()
        self._reflection_note = updated_note or self._reflection_note
