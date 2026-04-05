from __future__ import annotations

from textwrap import dedent

import pytest

from poker_bot.config import OpenRouterSettings, load_project_config


def test_load_project_config_resolves_matching_openrouter_settings(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(
            """
            [llm]
            model = "gpt-test"
            api_key = "test-key"
            base_url = "https://openrouter.ai/api/v1"

            [llm.openrouter]
            sort = "throughput"
            """
        ).strip()
    )

    config = load_project_config(config_path)

    assert isinstance(config.llm.provider_settings, OpenRouterSettings)
    assert config.llm.provider_settings.sort == "throughput"
    assert config.llm.to_extra_body() == {"provider": {"sort": "throughput"}}


def test_load_project_config_ignores_openrouter_settings_for_other_base_urls(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(
            """
            [llm]
            model = "gpt-test"
            api_key = "test-key"
            base_url = "https://api.openai.com/v1"

            [llm.openrouter]
            sort = "throughput"
            """
        ).strip()
    )

    config = load_project_config(config_path)

    assert config.llm.provider_settings is None
    assert config.llm.to_extra_body() == {}


def test_load_project_config_rejects_invalid_openrouter_sort(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(
            """
            [llm]
            model = "gpt-test"
            api_key = "test-key"
            base_url = "https://openrouter.ai/api/v1"

            [llm.openrouter]
            sort = "fastest"
            """
        ).strip()
    )

    with pytest.raises(ValueError, match="llm.openrouter.sort must be one of"):
        load_project_config(config_path)


def test_load_project_config_reads_web_showdown_delay(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(
            """
            [web]
            showdown_delay_seconds = 1.25
            """
        ).strip()
    )

    config = load_project_config(config_path)

    assert config.web.showdown_delay_seconds == 1.25


def test_load_project_config_rejects_negative_web_showdown_delay(tmp_path) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text(
        dedent(
            """
            [web]
            showdown_delay_seconds = -0.5
            """
        ).strip()
    )

    with pytest.raises(ValueError, match="web.showdown_delay_seconds must be >= 0"):
        load_project_config(config_path)
