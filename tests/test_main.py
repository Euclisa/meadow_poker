from __future__ import annotations

import asyncio

import pytest

from poker_bot.config import GameSettings, LLMSettings, ProjectConfig, TelegramSettings, ThoughtLoggingMode
from poker_bot.main import run_cli_mode
import poker_bot.main as main_module


class RecordingCLIPlayerAgent:
    def __init__(self, seat_id: str) -> None:
        self.seat_id = seat_id


class RecordingLLMGameClient:
    def __init__(
        self,
        *,
        settings: LLMSettings,
    ) -> None:
        self.settings = settings


class RecordingLLMPlayerAgent:
    def __init__(
        self,
        seat_id: str,
        client: RecordingLLMGameClient,
        recent_hand_count: int = 5,
        thought_logging: ThoughtLoggingMode = ThoughtLoggingMode.OFF,
    ) -> None:
        self.seat_id = seat_id
        self.client = client
        self.recent_hand_count = recent_hand_count
        self.thought_logging = thought_logging


class RecordingOrchestrator:
    last_instance: RecordingOrchestrator | None = None

    def __init__(self, engine, agents) -> None:
        self.engine = engine
        self.agents = agents
        RecordingOrchestrator.last_instance = self


def make_config(*, with_llm: bool = True) -> ProjectConfig:
    return ProjectConfig(
        game=GameSettings(),
        llm=LLMSettings(model="gpt-test", api_key="test") if with_llm else LLMSettings(),
        telegram=TelegramSettings(),
    )


def test_run_cli_mode_assigns_human_names_and_bot_seats(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    async def fake_run_table(orchestrator, *, max_hands: int, close_agents: bool = True, after_hand=None) -> None:
        captured["orchestrator"] = orchestrator
        captured["max_hands"] = max_hands
        captured["close_agents"] = close_agents
        captured["after_hand"] = after_hand

    monkeypatch.setattr(main_module, "CLIPlayerAgent", RecordingCLIPlayerAgent)
    monkeypatch.setattr(main_module, "LLMGameClient", RecordingLLMGameClient)
    monkeypatch.setattr(main_module, "LLMPlayerAgent", RecordingLLMPlayerAgent)
    monkeypatch.setattr(main_module, "GameOrchestrator", RecordingOrchestrator)
    monkeypatch.setattr(main_module, "run_table", fake_run_table)

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
    assert orchestrator.agents["p2"].thought_logging is ThoughtLoggingMode.OFF
    assert captured["max_hands"] == 7
    assert captured["close_agents"] is True
    assert captured["after_hand"] is None


def test_run_cli_mode_passes_llm_thought_logging(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_run_table(orchestrator, *, max_hands: int, close_agents: bool = True, after_hand=None) -> None:
        del orchestrator, max_hands, close_agents, after_hand

    monkeypatch.setattr(main_module, "CLIPlayerAgent", RecordingCLIPlayerAgent)
    monkeypatch.setattr(main_module, "LLMGameClient", RecordingLLMGameClient)
    monkeypatch.setattr(main_module, "LLMPlayerAgent", RecordingLLMPlayerAgent)
    monkeypatch.setattr(main_module, "GameOrchestrator", RecordingOrchestrator)
    monkeypatch.setattr(main_module, "run_table", fake_run_table)

    config = ProjectConfig(
        game=GameSettings(),
        llm=LLMSettings(
            model="gpt-test",
            api_key="test",
            thought_logging=ThoughtLoggingMode.NOTES,
        ),
        telegram=TelegramSettings(),
    )

    asyncio.run(run_cli_mode(config, players_spec="Alice,bot", max_hands=1))

    orchestrator = RecordingOrchestrator.last_instance
    assert orchestrator is not None
    assert orchestrator.agents["p2"].thought_logging is ThoughtLoggingMode.NOTES
    assert orchestrator.agents["p2"].client.settings.thought_logging is ThoughtLoggingMode.NOTES


def test_run_cli_mode_rejects_duplicate_human_names_case_insensitively() -> None:
    with pytest.raises(ValueError, match="Duplicate CLI user names are not allowed: alice"):
        asyncio.run(run_cli_mode(make_config(), players_spec="Alice,alice", max_hands=1))


def test_run_cli_mode_requires_llm_config_when_bot_seat_is_present() -> None:
    with pytest.raises(ValueError, match="when CLI uses bot seats"):
        asyncio.run(run_cli_mode(make_config(with_llm=False), players_spec="Alice,bot", max_hands=1))
