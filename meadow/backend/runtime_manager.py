from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from meadow.backend.human_agent import BackendHumanAgent
from meadow.backend.models import BackendTableRuntime, ShowdownReveal, ShowdownState, ShowdownWinner
from meadow.backend.runtime_state import BackendRuntimePublisher
from meadow.coach import TableCoach
from meadow.config import ThoughtLoggingMode
from meadow.llm_bot import LLMGameClient, LLMPlayerAgent
from meadow.player_agent import PlayerAgent
from meadow.poker.engine import PokerEngine
from meadow.table_runner import run_table
from meadow.types import DecisionRequest, PlayerAction, SeatConfig, TableConfig, TelegramTableState

logger = logging.getLogger(__name__)


class BackendRuntimeManager:
    def __init__(
        self,
        *,
        publisher: BackendRuntimePublisher,
        llm_client_factory: Callable[[], LLMGameClient] | None = None,
        coach_client_factory: Callable[[], LLMGameClient] | None = None,
        llm_name_allocator: Any | None = None,
        llm_recent_hand_count: int = 5,
        llm_thought_logging: ThoughtLoggingMode = ThoughtLoggingMode.OFF,
        coach_enabled: bool = False,
        coach_recent_hand_count: int = 5,
        showdown_delay_seconds: float = 0.0,
    ) -> None:
        self._publisher = publisher
        self._llm_client_factory = llm_client_factory
        self._coach_client_factory = coach_client_factory
        self._llm_name_allocator = llm_name_allocator
        self._llm_recent_hand_count = llm_recent_hand_count
        self._llm_thought_logging = llm_thought_logging
        self._coach_enabled = coach_enabled
        self._coach_recent_hand_count = coach_recent_hand_count
        self._showdown_delay_seconds = showdown_delay_seconds

    async def start_runtime(self, runtime: BackendTableRuntime) -> None:
        seat_configs: list[SeatConfig] = []
        player_agents: dict[str, PlayerAgent] = {}

        async def publish_state() -> None:
            await self._publisher.publish_runtime_state(runtime)

        async def handle_turn_timeout(decision: DecisionRequest, action: PlayerAction) -> None:
            del decision
            runtime.add_activity(
                kind="state",
                text=f"Time expired. Auto-{self._format_action_label(action)}.",
            )
            await self._publisher.publish_runtime_state(runtime)

        for reservation in runtime.reservations:
            if not reservation.is_seated or reservation.seat_id is None:
                continue
            seat_configs.append(SeatConfig(seat_id=reservation.seat_id, name=reservation.actor.display_name))
            human_agent = BackendHumanAgent(seat_id=reservation.seat_id, on_state_changed=publish_state)
            player_agents[reservation.seat_id] = human_agent
            runtime.human_agents[reservation.seat_id] = human_agent

        for index in range(1, runtime.llm_seat_count + 1):
            seat_id = f"llm_{index}"
            seat_configs.append(SeatConfig(seat_id=seat_id, name=self._allocate_llm_name()))
            player_agents[seat_id] = LLMPlayerAgent(
                seat_id=seat_id,
                client=self._require_llm_client_factory()(),
                recent_hand_count=self._llm_recent_hand_count,
                thought_logging=self._llm_thought_logging,
            )

        engine = PokerEngine.create_table(
            TableConfig(
                small_blind=runtime.config.small_blind,
                big_blind=runtime.config.big_blind,
                ante=runtime.config.ante,
                starting_stack=runtime.config.starting_stack,
                max_players=runtime.total_seats,
            ),
            seat_configs,
        )
        from meadow.orchestrator import GameOrchestrator

        orchestrator = GameOrchestrator(
            engine,
            player_agents,
            turn_timeout_seconds=runtime.config.turn_timeout_seconds,
            idle_close_seconds=runtime.config.idle_close_seconds,
            on_turn_state_changed=publish_state,
            on_turn_timeout=handle_turn_timeout,
        )
        runtime.engine = engine
        runtime.player_agents = player_agents
        runtime.orchestrator = orchestrator
        runtime.coach = self._build_table_coach()
        runtime.status = TelegramTableState.RUNNING
        runtime.status_message = f"Table {runtime.table_id} started with {runtime.total_seats} seats."
        runtime.add_activity(kind="state", text=runtime.status_message)
        await self._publisher.publish_runtime_state(runtime, waiting_tables_changed=True)
        runtime.orchestrator_task = asyncio.create_task(self.run_runtime(runtime))

    async def run_runtime(self, runtime: BackendTableRuntime) -> None:
        assert runtime.orchestrator is not None

        async def after_hand(result: Any) -> None:
            if result.completed_hand is not None and runtime.coach is not None:
                await runtime.coach.record_completed_hand(result.completed_hand)
            if not result.ended_in_showdown:
                return
            runtime.showdown_state = self._build_showdown_state(result)
            await self._publisher.publish_runtime_state(runtime)
            if self._showdown_delay_seconds > 0:
                await asyncio.sleep(self._showdown_delay_seconds)
                runtime.showdown_state = None
                await self._publisher.publish_runtime_state(runtime)

        try:
            await run_table(
                runtime.orchestrator,
                max_hands=runtime.config.max_hands_per_table,
                close_agents=True,
                after_hand=after_hand,
            )
        finally:
            logger.info("Backend table %s completed", runtime.table_id)
            runtime.status = TelegramTableState.COMPLETED
            runtime.status_message = f"Table {runtime.table_id} has completed."
            runtime.add_activity(kind="state", text=runtime.status_message)
            await self._publisher.publish_runtime_state(runtime, waiting_tables_changed=True)

    def _build_table_coach(self) -> TableCoach | None:
        if not self._coach_enabled:
            return None
        if self._coach_client_factory is None:
            raise RuntimeError("Coach client factory is required when coach is enabled")
        return TableCoach(
            self._coach_client_factory(),
            recent_hand_count=self._coach_recent_hand_count,
        )

    def _build_showdown_state(self, result: Any) -> ShowdownState:
        return ShowdownState(
            revealed_seats=tuple(
                ShowdownReveal(
                    seat_id=event.payload["seat_id"],
                    hole_cards=tuple(event.payload["hole_cards"]),
                )
                for event in result.events
                if event.event_type == "showdown_revealed"
            ),
            winners=tuple(
                ShowdownWinner(
                    seat_id=event.payload["seat_id"],
                    amount=event.payload["amount"],
                )
                for event in result.events
                if event.event_type == "pot_awarded"
            ),
        )

    def _allocate_llm_name(self) -> str:
        if self._llm_name_allocator is None:
            from meadow.naming import BotNameAllocator

            self._llm_name_allocator = BotNameAllocator()
        return self._llm_name_allocator.allocate()

    def _require_llm_client_factory(self) -> Callable[[], LLMGameClient]:
        if self._llm_client_factory is None:
            raise RuntimeError("LLM client factory is required to create LLM seats")
        return self._llm_client_factory

    @staticmethod
    def _format_action_label(action: PlayerAction) -> str:
        if action.amount is None:
            return action.action_type.value
        return f"{action.action_type.value} {action.amount}"
