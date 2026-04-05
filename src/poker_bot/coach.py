from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time

from poker_bot.hand_history import (
    render_live_public_hand_summary,
    render_public_completed_hand_summary,
)
from poker_bot.players.llm import LLMGameClient
from poker_bot.players.rendering import render_decision_summary
from poker_bot.types import DecisionRequest, HandRecord, HandTransition

logger = logging.getLogger(__name__)


class CoachRequestError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class PublicNoteRevision:
    effective_from_hand_number: int
    note: str


class TableCoach:
    _NOTE_PROMPT = (
        "You are a poker table coach reviewing public completed-hand summaries. "
        "Write only an updated public table note and nothing else. "
        "Track public tendencies, bet sizing patterns, showdown information, and table dynamics. "
        "Do not invent hole cards that were not publicly shown."
    )
    _ADVICE_PROMPT = (
        "You are a poker coach advising one player on their current turn. "
        "Use only the provided private view for that player, the live public hand summary, and the rolling public table note. "
        "Respond in natural prose with no bullets, markdown, or headings. "
        "Give a clear recommended action and a brief practical reason in 2-4 short sentences. "
        "Do not mention unseen opponent hole cards or claim certainty."
    )
    _REPLAY_ADVICE_PROMPT = (
        "You are a poker coach reviewing a replay from one player's perspective. "
        "Use only the provided private view at the selected replay step, the partial public hand summary through that step, "
        "the historical rolling public table note that was already available before this hand began, "
        "and the separately provided recorded next action. "
        "First explain the spot in general, then separately evaluate the recorded action. "
        "Respond in natural prose with no bullets, markdown, or headings. "
        "Keep it concise and practical in 3-5 short sentences. "
        "Do not mention future events beyond the recorded action, unseen opponent hole cards, or claim certainty."
    )

    def __init__(self, client: LLMGameClient, *, recent_hand_count: int = 5) -> None:
        self.client = client
        self.recent_hand_count = max(1, recent_hand_count)
        self._pending_public_summaries: list[str] = []
        self._pending_public_hand_numbers: list[int] = []
        self._rolling_public_note: str | None = None
        self._public_note_history: list[PublicNoteRevision] = []

    @property
    def rolling_public_note(self) -> str | None:
        return self._rolling_public_note

    @property
    def public_note_history(self) -> tuple[PublicNoteRevision, ...]:
        return tuple(self._public_note_history)

    async def record_completed_hand(self, record: HandRecord) -> None:
        summary = render_public_completed_hand_summary(record)
        self._pending_public_summaries.append(summary)
        self._pending_public_hand_numbers.append(record.hand_number)
        if len(self._pending_public_summaries) < self.recent_hand_count:
            return
        await self._update_public_note()

    def public_note_for_replay_hand(self, hand_number: int) -> str | None:
        note: str | None = None
        for revision in self._public_note_history:
            if revision.effective_from_hand_number > hand_number:
                break
            note = revision.note
        return note

    async def answer_question(
        self,
        *,
        table_id: str,
        seat_id: str,
        decision: DecisionRequest,
        current_hand_record: HandRecord,
        question: str,
    ) -> str:
        started_at = time.monotonic()
        messages = [{"role": "developer", "content": self._ADVICE_PROMPT}]
        if self._rolling_public_note:
            messages.append(
                {
                    "role": "developer",
                    "content": (
                        "Rolling public table note:\n\n"
                        f"{self._rolling_public_note}"
                    ),
                }
            )
        prompt = "\n\n".join(
            [
                "Player turn context:",
                render_decision_summary(decision),
                "Current public hand summary:",
                render_live_public_hand_summary(current_hand_record),
                "Player question:",
                question.strip(),
            ]
        )
        messages.append({"role": "user", "content": prompt})
        try:
            reply = await asyncio.wait_for(
                self.client.complete_text(messages),
                timeout=self.client.settings.timeout,
            )
        except TimeoutError as exc:
            logger.warning("Coach timeout table_id=%s seat_id=%s", table_id, seat_id)
            raise CoachRequestError("Coach took too long to reply. Please try again.") from exc
        except Exception as exc:
            logger.warning("Coach failed table_id=%s seat_id=%s", table_id, seat_id)
            raise CoachRequestError("Coach could not generate advice right now.") from exc
        duration_ms = int((time.monotonic() - started_at) * 1000)
        cleaned_reply = reply.strip()
        logger.info(
            "Coach reply table_id=%s seat_id=%s duration_ms=%s reply=%s",
            table_id,
            seat_id,
            duration_ms,
            cleaned_reply,
        )
        return cleaned_reply

    async def analyze_replay_spot(
        self,
        *,
        table_id: str,
        seat_id: str,
        decision: DecisionRequest,
        replay_hand_summary: str,
        next_transition: HandTransition,
        replay_hand_number: int,
    ) -> str:
        started_at = time.monotonic()
        messages = [{"role": "developer", "content": self._REPLAY_ADVICE_PROMPT}]
        historical_note = self.public_note_for_replay_hand(replay_hand_number)
        if historical_note:
            messages.append(
                {
                    "role": "developer",
                    "content": (
                        "Historical rolling public table note for this hand:\n\n"
                        f"{historical_note}"
                    ),
                }
            )
        prompt = "\n\n".join(
            [
                "Replay player context:",
                render_decision_summary(decision),
                "Replay hand summary through the selected step:",
                replay_hand_summary,
                "Recorded next action:",
                _render_recorded_transition(next_transition),
            ]
        )
        messages.append({"role": "user", "content": prompt})
        try:
            reply = await asyncio.wait_for(
                self.client.complete_text(messages),
                timeout=self.client.settings.timeout,
            )
        except TimeoutError as exc:
            logger.warning("Replay coach timeout table_id=%s seat_id=%s hand=%s", table_id, seat_id, replay_hand_number)
            raise CoachRequestError("Coach took too long to reply. Please try again.") from exc
        except Exception as exc:
            logger.warning("Replay coach failed table_id=%s seat_id=%s hand=%s", table_id, seat_id, replay_hand_number)
            raise CoachRequestError("Coach could not generate advice right now.") from exc
        duration_ms = int((time.monotonic() - started_at) * 1000)
        cleaned_reply = reply.strip()
        logger.info(
            "Replay coach reply table_id=%s seat_id=%s hand=%s duration_ms=%s reply=%s",
            table_id,
            seat_id,
            replay_hand_number,
            duration_ms,
            cleaned_reply,
        )
        return cleaned_reply

    async def _update_public_note(self) -> None:
        messages = [{"role": "developer", "content": self._NOTE_PROMPT}]
        prompt_parts = [
            "Completed public hand summaries to synthesize:",
            "\n\n".join(self._pending_public_summaries),
        ]
        if self._rolling_public_note is not None:
            prompt_parts.extend(
                [
                    "",
                    "Your current public table note:",
                    self._rolling_public_note,
                    "",
                    "Revise the note using the new completed hands.",
                ]
            )
        else:
            prompt_parts.extend(["", "Write the first public table note based on these hands."])
        messages.append({"role": "user", "content": "\n".join(prompt_parts)})
        try:
            updated_note = await asyncio.wait_for(
                self.client.complete_text(messages),
                timeout=self.client.settings.timeout,
            )
        except Exception:
            logger.warning("Coach public note update failed")
            return
        previous_note = self._rolling_public_note
        cleaned_note = updated_note.strip()
        self._rolling_public_note = cleaned_note
        effective_from_hand_number = max(self._pending_public_hand_numbers) + 1
        if cleaned_note and cleaned_note != previous_note:
            self._public_note_history.append(
                PublicNoteRevision(
                    effective_from_hand_number=effective_from_hand_number,
                    note=cleaned_note,
                )
            )
        self._pending_public_summaries.clear()
        self._pending_public_hand_numbers.clear()


def _render_recorded_transition(transition: HandTransition) -> str:
    action = transition.action
    seat_id = transition.seat_id or "unknown"
    if action is None:
        return f"{seat_id}: unknown action"
    if action.amount is not None:
        return f"{seat_id}: {action.action_type.value} {action.amount}"
    return f"{seat_id}: {action.action_type.value}"
