from __future__ import annotations

import asyncio

import pytest

from poker_bot.config import GameSettings, LLMSettings, ProjectConfig, TelegramSettings
from poker_bot.main import run_cli_mode
import poker_bot.main as main_module


class RecordingCLIPlayerAgent:
    def __init__(self, seat_id: str) -> None:
        self.seat_id = seat_id


class RecordingLLMGameClient:
    def __init__(
        self,
        *,
        model: str | None,
        api_key: str | None,
        base_url: str | None,
        timeout: float,
        max_output_tokens: int | None,
    ) -> None:
        self.model = model
        self.api_key = api_key
        self.base_url = base_url
        self.timeout = timeout
        self.max_output_tokens = max_output_tokens


class RecordingLLMPlayerAgent:
    def __init__(self, seat_id: str, client: RecordingLLMGameClient, recent_hand_count: int = 5) -> None:
        self.seat_id = seat_id
        self.client = client
        self.recent_hand_count = recent_hand_count


class RecordingOrchestrator:
    last_instance: RecordingOrchestrator | None = None

    def __init__(self, engine, agents) -> None:
        self.engine = engine
        self.agents = agents
        self.max_hands = None
        RecordingOrchestrator.last_instance = self

    async def run(self, *, max_hands: int) -> None:
        self.max_hands = max_hands


def make_config(*, with_llm: bool = True) -> ProjectConfig:
    return ProjectConfig(
        game=GameSettings(),
        llm=LLMSettings(model="gpt-test", api_key="test") if with_llm else LLMSettings(),
        telegram=TelegramSettings(),
    )


def test_run_cli_mode_assigns_human_names_and_bot_seats(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(main_module, "CLIPlayerAgent", RecordingCLIPlayerAgent)
    monkeypatch.setattr(main_module, "LLMGameClient", RecordingLLMGameClient)
    monkeypatch.setattr(main_module, "LLMPlayerAgent", RecordingLLMPlayerAgent)
    monkeypatch.setattr(main_module, "GameOrchestrator", RecordingOrchestrator)

    asyncio.run(run_cli_mode(make_config(), players_spec="Alice,bot,cli", max_hands=7))

    orchestrator = RecordingOrchestrator.last_instance
    assert orchestrator is not None
    seats = orchestrator.engine.get_public_table_view().seats
    assert seats[0].name == "Alice"
    assert seats[1].name.endswith("_bot")
    assert seats[2].name == "cli"
    assert isinstance(orchestrator.agents["p1"], RecordingCLIPlayerAgent)
    assert isinstance(orchestrator.agents["p2"], RecordingLLMPlayerAgent)
    assert isinstance(orchestrator.agents["p3"], RecordingCLIPlayerAgent)
    assert orchestrator.agents["p2"].recent_hand_count == 5
    assert orchestrator.max_hands == 7


def test_run_cli_mode_rejects_duplicate_human_names_case_insensitively() -> None:
    with pytest.raises(ValueError, match="Duplicate CLI user names are not allowed: alice"):
        asyncio.run(run_cli_mode(make_config(), players_spec="Alice,alice", max_hands=1))


def test_run_cli_mode_requires_llm_config_when_bot_seat_is_present() -> None:
    with pytest.raises(ValueError, match="when CLI uses bot seats"):
        asyncio.run(run_cli_mode(make_config(with_llm=False), players_spec="Alice,bot", max_hands=1))
