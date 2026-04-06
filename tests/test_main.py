from __future__ import annotations

import asyncio

import pytest

from poker_bot.backend.service import LocalBackendClient
from poker_bot.config import BackendSettings, GameSettings, LLMSettings, ProjectConfig, TelegramSettings, WebSettings
import poker_bot.main as main_module
from poker_bot.main import run_cli_mode

from support import make_backend_service


def make_config(*, with_llm: bool = True) -> ProjectConfig:
    return ProjectConfig(
        game=GameSettings(),
        llm=LLMSettings(model="gpt-test", api_key="test") if with_llm else LLMSettings(),
        backend=BackendSettings(),
        telegram=TelegramSettings(),
        web=WebSettings(),
    )


def test_run_cli_mode_supports_all_bot_tables(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    backend = LocalBackendClient(
        make_backend_service(
            llm_outputs=['{"action":"fold"}', '{"action":"check"}', '{"action":"check"}'],
        )
    )
    monkeypatch.setattr(main_module, "_build_backend_client", lambda config, showdown_delay_seconds=None: backend)

    asyncio.run(run_cli_mode(make_config(), players_spec="bot,bot", max_hands=1))

    output = capsys.readouterr().out
    assert "Hand #1" in output
    assert "Final standings" in output


def test_run_cli_mode_handles_human_and_bot_backend_flow(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    backend = LocalBackendClient(
        make_backend_service(
            llm_outputs=['{"action":"check"}', '{"action":"check"}', '{"action":"check"}'],
        )
    )
    monkeypatch.setattr(main_module, "_build_backend_client", lambda config, showdown_delay_seconds=None: backend)

    answers = iter(["fold"])

    async def fake_read_cli_text(prompt: str) -> str:
        del prompt
        return next(answers)

    monkeypatch.setattr(main_module, "_read_cli_text", fake_read_cli_text)

    asyncio.run(run_cli_mode(make_config(), players_spec="Alice,bot", max_hands=1, turn_timeout=15))

    output = capsys.readouterr().out
    assert "Turn timer: 15s" in output
    assert "Actions:" in output
    assert "Final standings" in output


def test_run_cli_mode_rejects_duplicate_human_names_case_insensitively() -> None:
    with pytest.raises(ValueError, match="Duplicate CLI user names are not allowed: alice"):
        asyncio.run(run_cli_mode(make_config(), players_spec="Alice,alice", max_hands=1))


def test_run_cli_mode_requires_llm_config_when_bot_seat_is_present() -> None:
    with pytest.raises(ValueError, match="when CLI uses bot seats"):
        asyncio.run(run_cli_mode(make_config(with_llm=False), players_spec="Alice,bot", max_hands=1))


def test_run_cli_mode_rejects_negative_ante() -> None:
    with pytest.raises(ValueError, match="ante must be non-negative"):
        asyncio.run(run_cli_mode(make_config(), players_spec="Alice,Bob", max_hands=1, ante=-1))
