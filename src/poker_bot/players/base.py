from __future__ import annotations

from abc import ABC, abstractmethod

from poker_bot.types import DecisionRequest, GameEvent, PlayerAction, PlayerView, PublicTableView


class PlayerAgent(ABC):
    seat_id: str

    @abstractmethod
    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        raise NotImplementedError

    @abstractmethod
    async def notify_terminal(
        self,
        events: tuple[GameEvent, ...],
        view: PlayerView | PublicTableView,
    ) -> None:
        raise NotImplementedError

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError
