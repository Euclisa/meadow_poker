from __future__ import annotations

from typing import Any

from meadow.backend.errors import BackendError
from meadow.backend.models import BackendTableRuntime, SeatReservation
from meadow.coach import CoachRequestError
from meadow.types import TelegramTableState


class BackendCoachService:
    async def request_coach(
        self,
        *,
        runtime: BackendTableRuntime,
        reservation: SeatReservation,
        table_id: str,
        question: str,
    ) -> dict[str, Any]:
        if runtime.status != TelegramTableState.RUNNING:
            raise BackendError("Coach tips are only available while the table is running.", status=400)
        if runtime.coach is None:
            raise BackendError("Coach is not enabled for this table.", status=400)
        assert reservation.seat_id is not None
        agent = runtime.human_agents.get(reservation.seat_id)
        if agent is None or agent.pending_decision is None:
            raise BackendError("Coach tips are only available on your turn.", status=400)
        orchestrator = runtime.orchestrator
        if orchestrator is None or orchestrator.current_hand_record is None:
            raise BackendError("Current hand context is unavailable.", status=400)
        try:
            reply = await runtime.coach.answer_question(
                table_id=table_id,
                seat_id=reservation.seat_id,
                decision=agent.pending_decision,
                current_hand_record=orchestrator.current_hand_record,
                question=question,
            )
        except CoachRequestError as exc:
            raise BackendError(str(exc), status=504) from exc
        return {"ok": True, "reply": reply}
