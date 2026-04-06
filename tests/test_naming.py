from __future__ import annotations

from meadow.naming import BotNameAllocator


def test_bot_name_allocator_adds_bot_suffix_and_uses_fallbacks() -> None:
    allocator = BotNameAllocator(names=("Ada", "Nova"), seed=1)

    names = [allocator.allocate(), allocator.allocate(), allocator.allocate()]

    assert names[0].endswith("_bot")
    assert names[1].endswith("_bot")
    assert set(names[:2]) == {"Ada_bot", "Nova_bot"}
    assert names[2] == "llm_3_bot"
