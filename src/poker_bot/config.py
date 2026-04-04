from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Mapping, Self
import tomllib
from urllib.parse import urlparse


DEFAULT_CONFIG_PATH = Path("config/config.toml")


@dataclass(frozen=True, slots=True)
class GameSettings:
    small_blind: int = 50
    big_blind: int = 100
    starting_stack: int = 2_000
    max_players: int = 6
    log_level: str | None = None


class LLMProviderSettings(ABC):
    section_name: ClassVar[str]

    @classmethod
    @abstractmethod
    def from_config(cls, raw: Mapping[str, object]) -> Self:
        raise NotImplementedError

    @abstractmethod
    def matches_base_url(self, base_url: str | None) -> bool:
        raise NotImplementedError

    @abstractmethod
    def to_extra_body(self) -> dict[str, Any]:
        raise NotImplementedError


@dataclass(frozen=True, slots=True)
class OpenRouterSettings(LLMProviderSettings):
    section_name: ClassVar[str] = "openrouter"

    sort: str | None = None

    @classmethod
    def from_config(cls, raw: Mapping[str, object]) -> Self:
        sort_raw = raw.get("sort")
        sort = None if sort_raw is None else str(sort_raw).strip().lower()
        if sort == "":
            sort = None
        if sort not in {None, "price", "throughput", "latency"}:
            raise ValueError("llm.openrouter.sort must be one of: price, throughput, latency")
        return cls(sort=sort)

    def matches_base_url(self, base_url: str | None) -> bool:
        host = _base_url_host(base_url)
        return host == "openrouter.ai" or host.endswith(".openrouter.ai")

    def to_extra_body(self) -> dict[str, Any]:
        if self.sort is None:
            return {}
        return {"provider": {"sort": self.sort}}


@dataclass(frozen=True, slots=True)
class LLMSettings:
    model: str | None = None
    api_key: str | None = None
    base_url: str | None = None
    timeout: float = 30.0
    max_output_tokens: int | None = None
    recent_hand_count: int = 5
    log_thoughts: bool = False
    provider_settings: LLMProviderSettings | None = None

    @classmethod
    def from_config(cls, raw: Mapping[str, object]) -> Self:
        base_url = _optional_str(raw.get("base_url"))
        provider_candidates = _parse_llm_provider_settings(raw)
        matching_providers = tuple(
            provider for provider in provider_candidates if provider.matches_base_url(base_url)
        )
        if len(matching_providers) > 1:
            provider_names = ", ".join(type(provider).__name__ for provider in matching_providers)
            raise ValueError(f"Multiple LLM provider settings match llm.base_url: {provider_names}")
        return cls(
            model=_optional_str(raw.get("model")),
            api_key=_optional_str(raw.get("api_key")),
            base_url=base_url,
            timeout=float(raw.get("timeout", 30.0)),
            max_output_tokens=_optional_int(raw.get("max_output_tokens")),
            recent_hand_count=int(raw.get("recent_hand_count", 5)),
            log_thoughts=bool(raw.get("log_thoughts", False)),
            provider_settings=matching_providers[0] if matching_providers else None,
        )

    def to_extra_body(self) -> dict[str, Any]:
        if self.provider_settings is None:
            return {}
        if not self.provider_settings.matches_base_url(self.base_url):
            return {}
        return self.provider_settings.to_extra_body()


@dataclass(frozen=True, slots=True)
class TelegramSettings:
    bot_token: str | None = None
    bot_username: str | None = None
    max_hands_per_table: int | None = None


@dataclass(frozen=True, slots=True)
class WebSettings:
    host: str = "127.0.0.1"
    port: int = 8080
    max_hands_per_table: int | None = None


@dataclass(frozen=True, slots=True)
class ProjectConfig:
    game: GameSettings = field(default_factory=GameSettings)
    llm: LLMSettings = field(default_factory=LLMSettings)
    telegram: TelegramSettings = field(default_factory=TelegramSettings)
    web: WebSettings = field(default_factory=WebSettings)


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
    web_raw = raw.get("web", {})
    game = GameSettings(
        small_blind=int(game_raw.get("small_blind", 50)),
        big_blind=int(game_raw.get("big_blind", 100)),
        starting_stack=int(game_raw.get("starting_stack", 2_000)),
        max_players=int(game_raw.get("max_players", 6)),
        log_level=game_raw.get("log_level"),
    )
    llm = LLMSettings.from_config(llm_raw)
    telegram = TelegramSettings(
        bot_token=telegram_raw.get("bot_token"),
        bot_username=telegram_raw.get("bot_username"),
        max_hands_per_table=_optional_int(telegram_raw.get("max_hands_per_table")),
    )
    web = WebSettings(
        host=str(web_raw.get("host", "127.0.0.1")),
        port=int(web_raw.get("port", 8080)),
        max_hands_per_table=_optional_int(web_raw.get("max_hands_per_table")),
    )

    _validate_project_config(game=game, telegram=telegram, web=web)
    return ProjectConfig(game=game, llm=llm, telegram=telegram, web=web)


def _validate_project_config(
    *,
    game: GameSettings,
    telegram: TelegramSettings,
    web: WebSettings,
) -> None:
    if game.max_players < 2:
        raise ValueError("game.max_players must be at least 2")
    if not web.host.strip():
        raise ValueError("web.host must not be empty")
    if not 0 < web.port < 65_536:
        raise ValueError("web.port must be between 1 and 65535")
    if telegram.bot_token is None:
        # Telegram mode may never be used locally, so keep it optional here.
        return


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    return str(value)


def _parse_llm_provider_settings(raw: Mapping[str, object]) -> tuple[LLMProviderSettings, ...]:
    providers: list[LLMProviderSettings] = []
    for provider_cls in _KNOWN_LLM_PROVIDER_SETTINGS:
        provider_raw = raw.get(provider_cls.section_name)
        if provider_raw is None:
            continue
        provider_table = _table_mapping(provider_raw, f"llm.{provider_cls.section_name}")
        providers.append(provider_cls.from_config(provider_table))
    return tuple(providers)


def _table_mapping(value: object, path: str) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be a TOML table")
    return value


def _base_url_host(base_url: str | None) -> str:
    if base_url is None:
        return ""
    parsed = urlparse(base_url)
    return parsed.hostname.casefold() if parsed.hostname else ""


_KNOWN_LLM_PROVIDER_SETTINGS: tuple[type[LLMProviderSettings], ...] = (OpenRouterSettings,)
