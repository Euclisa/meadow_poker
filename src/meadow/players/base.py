from __future__ import annotations

from abc import ABC, abstractmethod

from meadow.types import DecisionRequest, HandRecord, PlayerAction, PlayerUpdate, PlayerView


class PlayerAgent(ABC):
    seat_id: str

    @abstractmethod
    async def request_action(self, decision: DecisionRequest) -> PlayerAction:
        raise NotImplementedError

    @abstractmethod
    async def notify_update(self, update: PlayerUpdate) -> None:
        raise NotImplementedError

    async def on_hand_completed(self, record: HandRecord, player_view: PlayerView) -> None:
        pass

    @abstractmethod
    async def close(self) -> None:
        raise NotImplementedError
