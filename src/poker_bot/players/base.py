from __future__ import annotations

from abc import ABC, abstractmethod

from poker_bot.types import DecisionRequest, PlayerAction, PlayerUpdate


class PlayerAgent(ABC):
    seat_id: str

    @abstractmethod
    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        raise NotImplementedError

    @abstractmethod
    async def notify_update(self, update: PlayerUpdate) -> None:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError
