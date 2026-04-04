from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import tomllib


DEFAULT_CONFIG_PATH = Path("config/config.toml")


@dataclass(frozen=True, slots=True)
class GameSettings:
    small_blind: int = 50
    big_blind: int = 100
    starting_stack: int = 2_000
    max_players: int = 6


@dataclass(frozen=True, slots=True)
class LLMSettings:
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    timeout: float = 30.0
    max_output_tokens: int | None = None


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    bot_token: str | None = None
    bot_username: str | None = None
    max_hands_per_table: int | None = None


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    game: GameSettings
    llm: LLMSettings
    telegram: TelegramSettings


def load_project_config(path: str | Path = DEFAULT_CONFIG_PATH) -> ProjectConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found at {config_path}. Create it from config/config.toml.example."
        )

    with config_path.open("rb") as config_file:
        raw = tomllib.load(config_file)

    game_raw = raw.get("game", {})
    llm_raw = raw.get("llm", {})
    telegram_raw = raw.get("telegram", {})
    game = GameSettings(
        small_blind=int(game_raw.get("small_blind", 50)),
        big_blind=int(game_raw.get("big_blind", 100)),
        starting_stack=int(game_raw.get("starting_stack", 2_000)),
        max_players=int(game_raw.get("max_players", 6)),
    )
    llm = LLMSettings(
        model=llm_raw.get("model"),
        api_key=llm_raw.get("api_key"),
        base_url=llm_raw.get("base_url"),
        timeout=float(llm_raw.get("timeout", 30.0)),
        max_output_tokens=_optional_int(llm_raw.get("max_output_tokens")),
    )
    telegram = TelegramSettings(
        bot_token=telegram_raw.get("bot_token"),
        bot_username=telegram_raw.get("bot_username"),
        max_hands_per_table=_optional_int(telegram_raw.get("max_hands_per_table")),
    )

    _validate_project_config(game=game, telegram=telegram)
    return ProjectConfig(game=game, llm=llm, telegram=telegram)


def _validate_project_config(
    *,
    game: GameSettings,
    telegram: TelegramSettings,
) -> None:
    if game.max_players < 2:
        raise ValueError("game.max_players must be at least 2")
    if telegram.bot_token is None:
        # Telegram mode may never be used locally, so keep it optional here.
        return


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)
