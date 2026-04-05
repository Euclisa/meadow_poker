from __future__ import annotations

import json
import logging
from json import JSONDecodeError
from dataclasses import dataclass
from typing import Any

from poker_bot.config import LLMSettings, ThoughtLoggingMode
from poker_bot.players.base import PlayerAgent
from poker_bot.players.rendering import render_events, render_player_update
from poker_bot.types import (
    ActionType,
    DecisionRequest,
    GameEvent,
    PlayerAction,
    PlayerUpdate,
    PlayerUpdateType,
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
        settings: LLMSettings,
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
                "The openai package is required for LLMGameClient. Install poker-bot[llm]."
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
        self._hand_updates: list[PlayerUpdate] = []
        self._tracked_hand_number: int | None = None
        self._pending_hand_summaries: list[str] = []
        self._reflection_note: str | None = None

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
            self._track_hand_update(update)
            self._buffered_updates.append(update)
        if update.update_type in {PlayerUpdateType.HAND_COMPLETED, PlayerUpdateType.TABLE_COMPLETED}:
            if update.update_type == PlayerUpdateType.HAND_COMPLETED:
                await self._store_completed_hand_summary()
            self._current_hand_number = None
            self._history.clear()
            self._buffered_updates.clear()
        return None

    async def close(self) -> None:
        self._current_hand_number = None
        self._history.clear()
        self._buffered_updates.clear()
        self._hand_updates.clear()
        self._tracked_hand_number = None
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
        if self._current_hand_number != hand_number:
            self._current_hand_number = hand_number
            self._history.clear()
            self._buffered_updates = [
                update
                for update in self._buffered_updates
                if update.public_table_view.hand_number == hand_number
            ]

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
                "Updates since your last turn:",
                self._render_buffered_updates(),
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

    def _render_buffered_updates(self) -> str:
        if not self._buffered_updates:
            return "No updates."
        return "\n\n".join(render_player_update(update, compact=True) for update in self._buffered_updates)

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

    def _track_hand_update(self, update: PlayerUpdate) -> None:
        hand_number = update.public_table_view.hand_number
        if self._tracked_hand_number != hand_number:
            self._tracked_hand_number = hand_number
            self._hand_updates = []
        self._hand_updates.append(update)

    async def _store_completed_hand_summary(self) -> None:
        if not self._hand_updates or self.recent_hand_count <= 0:
            self._hand_updates.clear()
            self._tracked_hand_number = None
            return
        summary = self._summarize_hand(self._hand_updates)
        if self.thought_logging.logs_hand_summaries:
            logger.info("LLM thought seat_id=%s type=hand_summary\n%s", self.seat_id, summary)
        self._pending_hand_summaries.append(summary)
        if len(self._pending_hand_summaries) >= self.recent_hand_count:
            await self._update_reflection_note()
        self._hand_updates.clear()
        self._tracked_hand_number = None

    async def _update_reflection_note(self) -> None:
        messages = [{"role": "developer", "content": self._REFLECTION_PROMPT}]
        reflection_parts = [
            "Completed hand summaries to synthesize:",
            "\n\n".join(self._pending_hand_summaries),
        ]
        if self._reflection_note is not None:
            reflection_parts.extend(
                [
                    "",
                    "Your current observations:",
                    self._reflection_note,
                    "",
                    "Revise your observations using the new summaries.",
                ]
            )
        else:
            reflection_parts.extend(
                [
                    "",
                    "Write your first observations note based on these summaries.",
                ]
            )
        messages.append({"role": "user", "content": "\n".join(reflection_parts)})
        try:
            updated_note = await self.client.complete_text(messages)
        except Exception:
            logger.warning("LLM reflection note update failed seat_id=%s", self.seat_id)
            return
        self._reflection_note = updated_note.strip()
        if self.thought_logging.logs_reflection_notes and self._reflection_note:
            logger.info(
                "LLM thought seat_id=%s type=reflection_note\n%s",
                self.seat_id,
                self._reflection_note,
            )
        self._pending_hand_summaries.clear()

    def _summarize_hand(self, updates: list[PlayerUpdate]) -> str:
        first_update = updates[0]
        final_update = updates[-1]
        seat_names = {seat.seat_id: seat.name for seat in final_update.public_table_view.seats}
        events = [event for update in updates for event in update.events]
        player_view = final_update.player_view

        sections: list[str] = []
        current_heading = "Preflop:"
        current_lines: list[str] = []
        showdown_lines: list[str] = []
        result_lines: list[str] = []

        def flush_current_section() -> None:
            nonlocal current_lines
            if current_lines:
                sections.append("\n".join([current_heading, *current_lines]))
                current_lines = []

        for event in events:
            payload = event.payload
            if event.event_type == "street_started":
                phase = payload["phase"]
                if phase == "preflop":
                    continue
                flush_current_section()
                board = " ".join(payload.get("board_cards", ())) or "-"
                current_heading = f"{phase.title()}: {board}"
                continue
            if event.event_type == "showdown_started":
                flush_current_section()
                board = " ".join(payload.get("board_cards", ())) or "-"
                showdown_lines.append(f"- Final board: {board}")
                continue
            if event.event_type == "showdown_revealed":
                cards = " ".join(payload.get("hole_cards", ())) or "-"
                name = seat_names.get(payload.get("seat_id"), payload.get("seat_id", "unknown"))
                showdown_lines.append(f"- {name} showed {cards}: {payload['hand_label']}")
                continue
            if event.event_type in {"pot_awarded", "hand_awarded", "chips_refunded"}:
                result_lines.append(f"- {self._render_summary_event(event, seat_names)}")
                continue
            if event.event_type in {"hand_started", "hand_completed", "table_completed", "bet_updated"}:
                continue
            rendered = self._render_summary_event(event, seat_names)
            if rendered is not None:
                current_lines.append(f"- {rendered}")

        flush_current_section()

        lines = [
            f"Hand #{final_update.public_table_view.hand_number}",
            f"You were: {player_view.player_name} at seat {player_view.seat_id}",
            f"Your hole cards: {' '.join(player_view.hole_cards) or '-'}",
            "",
            "Players at hand start:",
            *[
                f"- {seat.seat_id} {seat.name}: stack={seat.stack}"
                for seat in first_update.public_table_view.seats
            ],
        ]
        if sections:
            lines.extend(["", *sections])
        if showdown_lines:
            lines.extend(["", "Showdown:", *showdown_lines])
        if result_lines:
            lines.extend(["", "Result:", *result_lines])
        lines.extend(
            [
                "",
                "Stacks after hand:",
                *[f"- {seat.name}: {seat.stack}" for seat in final_update.public_table_view.seats],
            ]
        )
        return "\n".join(lines)

    def _render_summary_event(self, event: GameEvent, seat_names: dict[str, str]) -> str | None:
        payload = event.payload
        seat_id = payload.get("seat_id")
        name = seat_names.get(seat_id, seat_id or "unknown")
        if event.event_type == "blind_posted":
            return f"{name} posted {payload['blind']} blind {payload['amount']}"
        if event.event_type == "action_applied":
            amount = payload.get("amount")
            action = payload["action"]
            if action == "raise" and amount is not None:
                return f"{name} raised to {amount}"
            if action == "bet" and amount is not None:
                return f"{name} bet {amount}"
            if action == "call" and amount is not None:
                return f"{name} called {amount}"
            if action == "fold":
                return f"{name} folded"
            if action == "check":
                return f"{name} checked"
            if amount is not None:
                return f"{name} {action} {amount}"
            return f"{name} {action}"
        if event.event_type == "pot_awarded":
            return f"{name} won {payload['amount']}"
        if event.event_type == "hand_awarded":
            return f"{name} collected {payload['amount']}"
        if event.event_type == "chips_refunded":
            return f"{name} refunded {payload['amount']}"
        return None
