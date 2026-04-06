from __future__ import annotations

import logging
from typing import Any

from meadow.backend.errors import BackendError
from meadow.backend.models import BackendTableRuntime, SeatReservation
from meadow.backend.serialization import serialize_replay_snapshot
from meadow.coach import CoachRequestError
from meadow.hand_history import render_replay_public_hand_summary
from meadow.replay import HandReplayBuildError, HandReplaySession, ReplayAnalysisError, build_replay_decision_spot

logger = logging.getLogger(__name__)


class BackendReplayService:
    async def get_replay_snapshot(
        self,
        *,
        runtime: BackendTableRuntime,
        viewer_token: str | None,
        table_id: str,
        hand_number: int,
        step_index: int,
    ) -> dict[str, Any]:
        archive = runtime.find_completed_hand_archive(hand_number)
        if archive is None:
            raise BackendError("Completed hand not found.", status=404)
        viewer = runtime.find_reservation_by_token(viewer_token)
        try:
            replay_session = HandReplaySession(
                archive.trace,
                viewer_seat_id=viewer.seat_id if viewer is not None else None,
            )
            frame = replay_session.materialize(step_index)
        except HandReplayBuildError as exc:
            logger.warning("Replay build failed table=%s hand=%s error=%s", table_id, hand_number, exc)
            raise BackendError("Replay could not be built for this hand.", status=500) from exc
        except IndexError as exc:
            raise BackendError("Replay step is out of range.", status=400) from exc
        return serialize_replay_snapshot(runtime, archive, frame, viewer_token=viewer_token)

    async def request_replay_coach(
        self,
        *,
        runtime: BackendTableRuntime,
        reservation: SeatReservation,
        table_id: str,
        hand_number: int,
        step_index: int,
    ) -> dict[str, Any]:
        if runtime.coach is None:
            raise BackendError("Coach is not enabled for this table.", status=400)
        assert reservation.seat_id is not None
        archive = runtime.find_completed_hand_archive(hand_number)
        if archive is None:
            raise BackendError("Completed hand not found.", status=404)
        try:
            spot = build_replay_decision_spot(
                archive.trace,
                step_index=step_index,
                viewer_seat_id=reservation.seat_id,
            )
        except HandReplayBuildError as exc:
            raise BackendError("Replay could not be built for this hand.", status=500) from exc
        except IndexError as exc:
            raise BackendError("Replay step is out of range.", status=400) from exc
        except ReplayAnalysisError as exc:
            raise BackendError(str(exc), status=400) from exc
        replay_hand_summary = render_replay_public_hand_summary(
            hand_number=archive.record.hand_number,
            events=spot.frame.visible_events,
            start_public_view=archive.record.start_public_view,
            current_public_view=spot.frame.public_table_view,
        )
        try:
            reply = await runtime.coach.analyze_replay_spot(
                table_id=table_id,
                seat_id=reservation.seat_id,
                decision=spot.decision,
                replay_hand_summary=replay_hand_summary,
                next_transition=spot.next_transition,
                replay_hand_number=archive.record.hand_number,
            )
        except CoachRequestError as exc:
            raise BackendError(str(exc), status=504) from exc
        return {"ok": True, "reply": reply}
