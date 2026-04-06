from __future__ import annotations

import random
from pathlib import Path


DEFAULT_BOT_NAMES_PATH = Path(__file__).resolve().parent / "data" / "names.txt"


def load_bot_names(path: Path = DEFAULT_BOT_NAMES_PATH) -> tuple[str, ...]:
    names = tuple(
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )
    if not names:
        raise ValueError(f"No bot names were found in {path}")
    return names


class BotNameAllocator:
    def __init__(self, names: tuple[str, ...] | None = None, *, seed: int | None = None) -> None:
        self._names = list(names or load_bot_names())
        self._random = random.Random(seed)
        self._random.shuffle(self._names)
        self._index = 0

    def allocate(self) -> str:
        if self._index < len(self._names):
            name = self._names[self._index]
            self._index += 1
            return f"{name}_bot"
        fallback_index = self._index + 1
        self._index += 1
        return f"llm_{fallback_index}_bot"
