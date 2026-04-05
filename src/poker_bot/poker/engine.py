from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Iterable

from poker_bot.poker.cards import best_hand_details, best_hand_rank
from poker_bot.poker.decks import (
    Deck,
    DeckExhaustedError,
    NoMoreDecksError,
    OrderedDeckFactory,
    ShuffledDeckFactory,
    encode_card_order,
)
from poker_bot.types import (
    ActionResult,
    ActionType,
    ActionValidationError,
    AutomaticProgressResult,
    DecisionRequest,
    GameEvent,
    GamePhase,
    HandSeatState,
    HandStateSnapshot,
    LegalAction,
    PlayerAction,
    PlayerView,
    PublicTableView,
    SeatConfig,
    SeatSnapshot,
    TableConfig,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class _SeatState:
    seat_id: str
    name: str
    stack: int
    hole_cards: list[str] = field(default_factory=list)
    folded: bool = False
    all_in: bool = False
    in_hand: bool = False
    committed_this_street: int = 0
    committed_this_hand: int = 0
    has_acted_this_round: bool = False
    last_action_bet_level: int = -1
    position: str | None = None

    def reset_for_hand(self) -> None:
        self.hole_cards = []
        self.folded = False
        self.all_in = False
        self.in_hand = self.stack > 0
        self.committed_this_street = 0
        self.committed_this_hand = 0
        self.has_acted_this_round = False
        self.last_action_bet_level = -1
        self.position = None


class PokerEngine:
    """Pure state machine for a single in-memory no-limit Texas Hold'em table."""

    def __init__(self, config: TableConfig, seats: Iterable[SeatConfig]) -> None:
        self.config = config
        self._seats = [
            _SeatState(
                seat_id=seat.seat_id,
                name=seat.name,
                stack=seat.starting_stack or config.starting_stack,
            )
            for seat in seats
        ]
        if not (config.min_players <= len(self._seats) <= config.max_players):
            raise ValueError("Seat count must be within configured player bounds")

        self._seat_by_id = {seat.seat_id: seat for seat in self._seats}
        if len(self._seat_by_id) != len(self._seats):
            raise ValueError("Seat ids must be unique")

        self._phase = GamePhase.READY_FOR_HAND
        self._hand_number = 0
        self._dealer_index = -1
        self._acting_index: int | None = None
        self._small_blind_index: int | None = None
        self._big_blind_index: int | None = None
        self._deck_factory = config.deck_factory or ShuffledDeckFactory()
        self._deck: Deck | None = None
        self._board_cards: list[str] = []
        self._current_bet = 0
        self._last_full_raise_amount = config.big_blind
        self._last_full_raise_to = 0

    @classmethod
    def create_table(cls, config: TableConfig, seats: Iterable[SeatConfig]) -> "PokerEngine":
        return cls(config=config, seats=seats)

    @classmethod
    def from_hand_state_snapshot(cls, snapshot: HandStateSnapshot) -> "PokerEngine":
        seats = [
            SeatConfig(seat_id=seat.seat_id, name=seat.name, starting_stack=max(1, seat.stack))
            for seat in snapshot.seats
        ]
        engine = cls(
            config=TableConfig(
                small_blind=snapshot.small_blind,
                big_blind=snapshot.big_blind,
                starting_stack=1,
                min_players=2,
                max_players=max(2, len(seats)),
                deck_factory=OrderedDeckFactory(snapshot.remaining_deck_order),
            ),
            seats=seats,
        )
        engine._phase = snapshot.phase
        engine._hand_number = snapshot.hand_number
        engine._board_cards = list(snapshot.board_cards)
        engine._current_bet = snapshot.current_bet
        engine._last_full_raise_amount = snapshot.last_full_raise_amount
        engine._last_full_raise_to = snapshot.last_full_raise_to
        engine._dealer_index = engine._index_of_optional_seat(snapshot.dealer_seat_id, default=-1)
        engine._acting_index = engine._index_of_optional_seat(snapshot.acting_seat_id)
        engine._small_blind_index = engine._index_of_optional_seat(snapshot.small_blind_seat_id)
        engine._big_blind_index = engine._index_of_optional_seat(snapshot.big_blind_seat_id)
        engine._deck = OrderedDeckFactory(snapshot.remaining_deck_order).create_hand_deck(snapshot.hand_number)

        for snapshot_seat in snapshot.seats:
            seat = engine._require_seat(snapshot_seat.seat_id)
            seat.stack = snapshot_seat.stack
            seat.hole_cards = list(snapshot_seat.hole_cards)
            seat.folded = snapshot_seat.folded
            seat.all_in = snapshot_seat.all_in
            seat.in_hand = snapshot_seat.in_hand
            seat.committed_this_street = snapshot_seat.committed_this_street
            seat.committed_this_hand = snapshot_seat.committed_this_hand
            seat.has_acted_this_round = snapshot_seat.has_acted_this_round
            seat.last_action_bet_level = snapshot_seat.last_action_bet_level
            seat.position = snapshot_seat.position
        return engine

    def get_phase(self) -> GamePhase:
        return self._phase

    def get_acting_seat(self) -> str | None:
        if self.has_pending_automatic_progress():
            return None
        self._ensure_acting_index_ready()
        if self._acting_index is None:
            return None
        return self._seats[self._acting_index].seat_id

    def get_public_table_view(self) -> PublicTableView:
        return PublicTableView(
            hand_number=self._hand_number,
            phase=self._phase,
            board_cards=tuple(self._board_cards),
            pot_total=sum(seat.committed_this_hand for seat in self._seats),
            current_bet=self._current_bet,
            dealer_seat_id=self._seat_id_or_none(self._dealer_index),
            acting_seat_id=self.get_acting_seat(),
            small_blind=self.config.small_blind,
            big_blind=self.config.big_blind,
            seats=tuple(self._seat_snapshot(seat) for seat in self._seats),
        )

    def get_player_view(self, seat_id: str) -> PlayerView:
        seat = self._require_seat(seat_id)
        return PlayerView(
            seat_id=seat.seat_id,
            player_name=seat.name,
            hole_cards=tuple(seat.hole_cards),
            stack=seat.stack,
            contribution=seat.committed_this_hand,
            position=seat.position,
            to_call=max(0, self._current_bet - seat.committed_this_street),
            public_table=self.get_public_table_view(),
        )

    def export_hand_state_snapshot(self) -> HandStateSnapshot:
        if self._phase not in {
            GamePhase.PREFLOP,
            GamePhase.FLOP,
            GamePhase.TURN,
            GamePhase.RIVER,
            GamePhase.SHOWDOWN,
            GamePhase.HAND_COMPLETE,
            GamePhase.TABLE_COMPLETE,
        }:
            raise RuntimeError("Hand state snapshots can only be exported for an active or completed hand")
        if self._deck is None:
            raise RuntimeError("Hand deck has not been initialized")
        return HandStateSnapshot(
            hand_number=self._hand_number,
            phase=self._phase,
            board_cards=tuple(self._board_cards),
            current_bet=self._current_bet,
            last_full_raise_amount=self._last_full_raise_amount,
            last_full_raise_to=self._last_full_raise_to,
            dealer_seat_id=self._seat_id_or_none(self._dealer_index),
            acting_seat_id=self._seat_id_or_none(self._acting_index),
            small_blind_seat_id=self._seat_id_or_none(self._small_blind_index),
            big_blind_seat_id=self._seat_id_or_none(self._big_blind_index),
            small_blind=self.config.small_blind,
            big_blind=self.config.big_blind,
            remaining_deck_order=encode_card_order(self._deck.card_order()),
            seats=tuple(self._hand_seat_state(seat) for seat in self._seats),
        )

    def export_remaining_deck_order(self) -> str:
        if self._deck is None:
            raise RuntimeError("Hand deck has not been initialized")
        return encode_card_order(self._deck.card_order())

    def get_decision_request(
        self,
        seat_id: str,
        validation_error: ActionValidationError | None = None,
    ) -> DecisionRequest:
        return DecisionRequest(
            acting_seat_id=seat_id,
            player_view=self.get_player_view(seat_id),
            public_table_view=self.get_public_table_view(),
            legal_actions=self.get_legal_actions(seat_id),
            validation_error=validation_error,
        )

    def get_legal_actions(self, seat_id: str) -> tuple[LegalAction, ...]:
        if self.has_pending_automatic_progress():
            return ()
        self._ensure_acting_index_ready()
        seat = self._require_seat(seat_id)
        if self.get_acting_seat() != seat_id:
            return ()
        if seat.folded or seat.all_in or not seat.in_hand:
            return ()

        legal_actions: list[LegalAction] = []
        to_call = max(0, self._current_bet - seat.committed_this_street)
        max_total = seat.committed_this_street + seat.stack

        if to_call > 0:
            legal_actions.append(LegalAction(ActionType.FOLD))
            legal_actions.append(LegalAction(ActionType.CALL))
            if max_total > self._current_bet and self._can_raise(seat):
                min_full_raise = self._current_bet + self._last_full_raise_amount
                if max_total < min_full_raise:
                    legal_actions.append(
                        LegalAction(
                            ActionType.RAISE,
                            min_amount=max_total,
                            max_amount=max_total,
                        )
                    )
                else:
                    legal_actions.append(
                        LegalAction(
                            ActionType.RAISE,
                            min_amount=min_full_raise,
                            max_amount=max_total,
                        )
                    )
        else:
            legal_actions.append(LegalAction(ActionType.CHECK))
            if seat.stack > 0:
                min_bet = min(self.config.big_blind, max_total)
                legal_actions.append(
                    LegalAction(ActionType.BET, min_amount=min_bet, max_amount=max_total)
                )
        return tuple(legal_actions)

    def start_next_hand(self, *, auto_resolve: bool = True) -> ActionResult:
        logger.info("Starting hand %s", self._hand_number + 1)
        if not self.is_table_active():
            return self._terminate_table(
                code="table_complete",
                message="Not enough funded players remain to start another hand",
                reason="not_enough_players",
            )

        try:
            self._deck = self._deck_factory.create_hand_deck(self._hand_number + 1)
        except NoMoreDecksError:
            return self._terminate_table(
                code="no_more_hands",
                message="The deck factory cannot provide another hand",
                reason="no_more_hands",
            )

        for seat in self._seats:
            seat.reset_for_hand()

        self._board_cards = []
        self._hand_number += 1
        self._phase = GamePhase.PREFLOP
        self._current_bet = 0
        self._last_full_raise_amount = self.config.big_blind
        self._last_full_raise_to = 0
        self._acting_index = None

        self._dealer_index = self._next_active_index(self._dealer_index)
        self._assign_positions()
        try:
            self._deal_private_cards()
        except DeckExhaustedError:
            return self._terminate_table(
                code="deck_exhausted",
                message="The hand deck ran out of cards before the hand could start",
                reason="deck_exhausted",
                refund_chips=True,
            )

        events: list[GameEvent] = [
            GameEvent(
                "hand_started",
                {
                    "hand_number": self._hand_number,
                    "dealer_seat_id": self._seat_id_or_none(self._dealer_index),
                },
            )
        ]
        events.extend(self._post_blinds())
        events.append(
            GameEvent(
                "street_started",
                {"phase": self._phase.value, "board_cards": tuple(self._board_cards)},
            )
        )

        self._acting_index = self._first_to_act_preflop()
        logger.info(
            "Hand %s started dealer=%s first_actor=%s",
            self._hand_number,
            self._seat_id_or_none(self._dealer_index),
            self.get_acting_seat(),
        )
        logger.debug(
            "Hand %s deal details board=%s hole_cards=%s",
            self._hand_number,
            self._board_cards,
            {seat.seat_id: tuple(seat.hole_cards) for seat in self._seats if seat.in_hand},
        )
        if auto_resolve:
            events.extend(self.drain_automatic_progress())
        return ActionResult(ok=True, events=tuple(events), state_changed=True)

    def apply_action(self, seat_id: str, action: PlayerAction, *, auto_resolve: bool = True) -> ActionResult:
        logger.debug(
            "Engine apply_action seat_id=%s action=%s phase=%s current_bet=%s board=%s",
            seat_id,
            action,
            self._phase,
            self._current_bet,
            self._board_cards,
        )
        if self._phase not in {
            GamePhase.PREFLOP,
            GamePhase.FLOP,
            GamePhase.TURN,
            GamePhase.RIVER,
        }:
            return self._invalid("wrong_phase", "A player action is not allowed in the current phase")
        if self.has_pending_automatic_progress():
            return self._invalid(
                "automatic_progress_pending",
                "Automatic table progress must be resolved before another player action",
            )
        self._ensure_acting_index_ready()

        if self.get_acting_seat() != seat_id:
            return self._invalid("not_your_turn", "Only the acting seat can submit an action")

        seat = self._require_seat(seat_id)
        validation_error = self._validate_action(seat, action)
        if validation_error is not None:
            return ActionResult(ok=False, error=validation_error, state_changed=False)

        previous_bet = self._current_bet
        events: list[GameEvent] = []

        if action.action_type == ActionType.FOLD:
            seat.folded = True
            seat.has_acted_this_round = True
            seat.last_action_bet_level = self._current_bet
            events.append(
                GameEvent("action_applied", {"seat_id": seat_id, "action": action.action_type.value})
            )
        elif action.action_type == ActionType.CHECK:
            seat.has_acted_this_round = True
            seat.last_action_bet_level = self._current_bet
            events.append(
                GameEvent("action_applied", {"seat_id": seat_id, "action": action.action_type.value})
            )
        elif action.action_type == ActionType.CALL:
            call_amount = min(seat.stack, self._current_bet - seat.committed_this_street)
            self._commit_chips(seat, call_amount)
            seat.has_acted_this_round = True
            seat.last_action_bet_level = self._current_bet
            events.append(
                GameEvent(
                    "action_applied",
                    {
                        "seat_id": seat_id,
                        "action": action.action_type.value,
                        "amount": call_amount,
                    },
                )
            )
        elif action.action_type == ActionType.BET:
            assert action.amount is not None
            delta = action.amount - seat.committed_this_street
            self._commit_chips(seat, delta)
            seat.has_acted_this_round = True
            seat.last_action_bet_level = action.amount
            self._current_bet = action.amount
            self._last_full_raise_amount = action.amount
            self._last_full_raise_to = action.amount
            events.append(
                GameEvent(
                    "action_applied",
                    {
                        "seat_id": seat_id,
                        "action": action.action_type.value,
                        "amount": action.amount,
                    },
                )
            )
        elif action.action_type == ActionType.RAISE:
            assert action.amount is not None
            delta = action.amount - seat.committed_this_street
            self._commit_chips(seat, delta)
            seat.has_acted_this_round = True
            seat.last_action_bet_level = action.amount
            raise_size = action.amount - self._current_bet
            self._current_bet = action.amount
            if raise_size >= self._last_full_raise_amount:
                self._last_full_raise_amount = raise_size
                self._last_full_raise_to = action.amount
            events.append(
                GameEvent(
                    "action_applied",
                    {
                        "seat_id": seat_id,
                        "action": action.action_type.value,
                        "amount": action.amount,
                    },
                )
            )
        else:
            return self._invalid("unsupported_action", "Unsupported action type")

        self._acting_index = self._next_action_index_from(self._index_of_seat(seat_id))
        if auto_resolve:
            events.extend(self.drain_automatic_progress())
        if self._phase in {GamePhase.PREFLOP, GamePhase.FLOP, GamePhase.TURN, GamePhase.RIVER}:
            if previous_bet != self._current_bet:
                events.append(GameEvent("bet_updated", {"current_bet": self._current_bet}))
        logger.info("Action seat=%s %s next=%s", seat_id, action.action_type.value, self.get_acting_seat())
        logger.debug(
            "Action details seat=%s action=%s phase=%s stacks=%s",
            seat_id,
            action,
            self._phase,
            {seat.seat_id: seat.stack for seat in self._seats},
        )
        return ActionResult(ok=True, events=tuple(events), state_changed=True)

    def is_hand_complete(self) -> bool:
        return self._phase == GamePhase.HAND_COMPLETE

    def is_table_active(self) -> bool:
        return sum(1 for seat in self._seats if seat.stack > 0) >= self.config.min_players

    def has_pending_automatic_progress(self) -> bool:
        return self._next_automatic_transition_kind() is not None

    def resolve_automatic_step(self) -> AutomaticProgressResult:
        transition_kind = self._next_automatic_transition_kind()
        if transition_kind is None:
            self._ensure_acting_index_ready()
            return AutomaticProgressResult(advanced=False)

        events: list[GameEvent] = []
        try:
            if transition_kind == "award_last_live_seat":
                self._award_last_live_seat(events)
            elif transition_kind == "advance_street":
                self._advance_street(events)
            elif transition_kind == "showdown":
                self._run_showdown(events)
            else:  # pragma: no cover - defensive against invalid helper values
                raise RuntimeError(f"Unsupported automatic transition: {transition_kind}")
        except DeckExhaustedError:
            result = self._terminate_table(
                code="deck_exhausted",
                message="The hand deck ran out of cards before the hand could finish",
                reason="deck_exhausted",
                events=events,
                refund_chips=True,
            )
            return AutomaticProgressResult(advanced=True, events=result.events)
        return AutomaticProgressResult(advanced=True, events=tuple(events))

    def drain_automatic_progress(self) -> tuple[GameEvent, ...]:
        drained: list[GameEvent] = []
        while True:
            progress = self.resolve_automatic_step()
            if not progress.advanced:
                break
            drained.extend(progress.events)
        return tuple(drained)

    def _validate_action(
        self,
        seat: _SeatState,
        action: PlayerAction,
    ) -> ActionValidationError | None:
        legal = {item.action_type: item for item in self.get_legal_actions(seat.seat_id)}
        if action.action_type not in legal:
            return ActionValidationError(
                code="illegal_action",
                message=f"{action.action_type.value} is not allowed right now",
            )

        bounds = legal[action.action_type]
        if action.action_type in {ActionType.BET, ActionType.RAISE}:
            if action.amount is None:
                return ActionValidationError(
                    code="missing_amount",
                    message="Bet and raise actions require an amount",
                )
            if bounds.min_amount is not None and action.amount < bounds.min_amount:
                return ActionValidationError(
                    code="amount_too_small",
                    message=f"Minimum allowed amount is {bounds.min_amount}",
                )
            if bounds.max_amount is not None and action.amount > bounds.max_amount:
                return ActionValidationError(
                    code="amount_too_large",
                    message=f"Maximum allowed amount is {bounds.max_amount}",
                )
        elif action.amount is not None:
            return ActionValidationError(
                code="unexpected_amount",
                message="This action must not include an amount",
            )
        return None

    def _next_automatic_transition_kind(self) -> str | None:
        if self._phase not in {
            GamePhase.PREFLOP,
            GamePhase.FLOP,
            GamePhase.TURN,
            GamePhase.RIVER,
        }:
            return None

        live_seats = [seat for seat in self._seats if seat.in_hand and not seat.folded]
        if len(live_seats) == 1:
            return "award_last_live_seat"

        actionable = [seat for seat in live_seats if not seat.all_in]
        if not actionable:
            return "showdown" if self._phase == GamePhase.RIVER else "advance_street"

        if len(actionable) == 1 and actionable[0].committed_this_street == self._current_bet:
            return "showdown" if self._phase == GamePhase.RIVER else "advance_street"

        if self._betting_round_complete():
            return "showdown" if self._phase == GamePhase.RIVER else "advance_street"

        return None

    def _ensure_acting_index_ready(self) -> None:
        if self._phase not in {
            GamePhase.PREFLOP,
            GamePhase.FLOP,
            GamePhase.TURN,
            GamePhase.RIVER,
        }:
            return
        if self._acting_index is not None or self.has_pending_automatic_progress():
            return
        self._acting_index = self._next_action_index_from(self._big_blind_index or 0)

    def _award_last_live_seat(self, events: list[GameEvent]) -> None:
        live_seats = [seat for seat in self._seats if seat.in_hand and not seat.folded]
        if len(live_seats) != 1:
            raise RuntimeError("Cannot award a single remaining seat when the hand is still contested")
        winner = live_seats[0]
        payout = sum(seat.committed_this_hand for seat in self._seats)
        winner.stack += payout
        events.append(
            GameEvent(
                "hand_awarded",
                {"seat_id": winner.seat_id, "amount": payout, "reason": "everyone_else_folded"},
            )
        )
        events.append(GameEvent("hand_completed", {"hand_number": self._hand_number}))
        self._phase = GamePhase.HAND_COMPLETE
        self._acting_index = None

    def _run_showdown(self, events: list[GameEvent]) -> None:
        self._phase = GamePhase.SHOWDOWN
        events.append(GameEvent("showdown_started", {"board_cards": tuple(self._board_cards)}))
        for seat in self._seats:
            if not seat.in_hand or seat.folded:
                continue
            _rank, hand_label = best_hand_details(tuple(seat.hole_cards + self._board_cards))
            events.append(
                GameEvent(
                    "showdown_revealed",
                    {
                        "seat_id": seat.seat_id,
                        "hole_cards": tuple(seat.hole_cards),
                        "hand_label": hand_label,
                    },
                )
            )
        payouts = self._calculate_showdown_payouts()
        logger.info("Showdown payouts=%s", payouts)
        for seat_id, amount in payouts.items():
            if amount <= 0:
                continue
            self._seat_by_id[seat_id].stack += amount
            events.append(GameEvent("pot_awarded", {"seat_id": seat_id, "amount": amount}))
        events.append(GameEvent("hand_completed", {"hand_number": self._hand_number}))
        self._phase = GamePhase.HAND_COMPLETE
        self._acting_index = None

    def _calculate_showdown_payouts(self) -> dict[str, int]:
        active = {
            seat.seat_id: best_hand_rank(tuple(seat.hole_cards + self._board_cards))
            for seat in self._seats
            if seat.in_hand and not seat.folded
        }
        payouts = {seat.seat_id: 0 for seat in self._seats}
        for amount, eligible in self._build_side_pots():
            eligible_live = [seat_id for seat_id in eligible if seat_id in active]
            if not eligible_live:
                continue
            winning_rank = max(active[seat_id] for seat_id in eligible_live)
            winners = [seat_id for seat_id in eligible_live if active[seat_id] == winning_rank]
            share, remainder = divmod(amount, len(winners))
            for winner in winners:
                payouts[winner] += share
            for winner in self._odd_chip_order(winners)[:remainder]:
                payouts[winner] += 1
        return payouts

    def _build_side_pots(self) -> list[tuple[int, list[str]]]:
        levels = sorted({seat.committed_this_hand for seat in self._seats if seat.committed_this_hand > 0})
        pots: list[tuple[int, list[str]]] = []
        previous_level = 0
        for level in levels:
            contributors = [seat for seat in self._seats if seat.committed_this_hand >= level]
            pot_amount = (level - previous_level) * len(contributors)
            eligible = [seat.seat_id for seat in contributors if not seat.folded and seat.in_hand]
            pots.append((pot_amount, eligible))
            previous_level = level
        return pots

    def _odd_chip_order(self, winners: list[str]) -> list[str]:
        if self._dealer_index is None:
            return winners
        ordered: list[str] = []
        start_index = self._next_occupied_index(self._dealer_index)
        current = start_index
        seen = set()
        while current is not None and len(seen) < len(self._seats):
            seat_id = self._seats[current].seat_id
            if seat_id in winners and seat_id not in seen:
                ordered.append(seat_id)
            seen.add(seat_id)
            current = self._next_occupied_index(current)
            if current == start_index:
                break
        return ordered or winners

    def _advance_street(self, events: list[GameEvent]) -> None:
        next_phase = {
            GamePhase.PREFLOP: GamePhase.FLOP,
            GamePhase.FLOP: GamePhase.TURN,
            GamePhase.TURN: GamePhase.RIVER,
        }[self._phase]
        self._phase = next_phase
        self._current_bet = 0
        self._last_full_raise_amount = self.config.big_blind
        self._last_full_raise_to = 0
        for seat in self._seats:
            seat.committed_this_street = 0
            seat.has_acted_this_round = False
            seat.last_action_bet_level = -1

        cards_to_deal = 3 if next_phase == GamePhase.FLOP else 1
        for _ in range(cards_to_deal):
            self._board_cards.append(self._draw_card())

        events.append(
            GameEvent(
                "street_started",
                {"phase": self._phase.value, "board_cards": tuple(self._board_cards)},
            )
        )
        self._acting_index = self._next_action_index_from(self._dealer_index)

    def _betting_round_complete(self) -> bool:
        actionable = [seat for seat in self._seats if seat.in_hand and not seat.folded and not seat.all_in]
        if not actionable:
            return True
        return all(
            seat.has_acted_this_round and seat.committed_this_street == self._current_bet
            for seat in actionable
        )

    def _can_raise(self, seat: _SeatState) -> bool:
        return (not seat.has_acted_this_round) or seat.last_action_bet_level < self._last_full_raise_to

    def _post_blinds(self) -> list[GameEvent]:
        events: list[GameEvent] = []
        active_count = sum(1 for seat in self._seats if seat.in_hand)
        if active_count == 2:
            self._small_blind_index = self._dealer_index
            self._big_blind_index = self._next_active_index(self._dealer_index)
        else:
            self._small_blind_index = self._next_active_index(self._dealer_index)
            self._big_blind_index = self._next_active_index(self._small_blind_index)

        small_blind_seat = self._seats[self._small_blind_index]
        big_blind_seat = self._seats[self._big_blind_index]
        small_blind = min(self.config.small_blind, small_blind_seat.stack)
        big_blind = min(self.config.big_blind, big_blind_seat.stack)
        self._commit_chips(small_blind_seat, small_blind)
        self._commit_chips(big_blind_seat, big_blind)
        self._current_bet = max(small_blind_seat.committed_this_street, big_blind_seat.committed_this_street)
        self._last_full_raise_to = self._current_bet

        events.append(
            GameEvent(
                "blind_posted",
                {"seat_id": small_blind_seat.seat_id, "blind": "small", "amount": small_blind},
            )
        )
        events.append(
            GameEvent(
                "blind_posted",
                {"seat_id": big_blind_seat.seat_id, "blind": "big", "amount": big_blind},
            )
        )
        return events

    def _first_to_act_preflop(self) -> int | None:
        active_count = sum(1 for seat in self._seats if seat.in_hand)
        if active_count == 2:
            return self._dealer_index
        return self._next_action_index_from(self._big_blind_index)

    def _deal_private_cards(self) -> None:
        active_seats = [seat for seat in self._seats if seat.in_hand]
        for _ in range(2):
            for seat in active_seats:
                seat.hole_cards.append(self._draw_card())

    def _assign_positions(self) -> None:
        active_indices = self._active_indices()
        if len(active_indices) == 2:
            self._seats[self._dealer_index].position = "dealer"
            other_index = self._next_active_index(self._dealer_index)
            self._seats[other_index].position = "big_blind"
            return

        labels = ["dealer", "small_blind", "big_blind", "under_the_gun", "middle_position", "cutoff"]
        ordered_indices = [self._dealer_index]
        current = self._dealer_index
        for _ in range(len(active_indices) - 1):
            current = self._next_active_index(current)
            ordered_indices.append(current)
        for label, index in zip(labels, ordered_indices, strict=False):
            self._seats[index].position = label

    def _commit_chips(self, seat: _SeatState, amount: int) -> None:
        if amount < 0 or amount > seat.stack:
            raise ValueError("Invalid chip movement")
        seat.stack -= amount
        seat.committed_this_street += amount
        seat.committed_this_hand += amount
        if seat.stack == 0:
            seat.all_in = True

    def _draw_card(self) -> str:
        if self._deck is None:
            raise RuntimeError("Hand deck has not been initialized")
        return self._deck.draw()

    def _terminate_table(
        self,
        *,
        code: str,
        message: str,
        reason: str,
        events: list[GameEvent] | None = None,
        refund_chips: bool = False,
    ) -> ActionResult:
        self._phase = GamePhase.TABLE_COMPLETE
        self._acting_index = None
        result_events = list(events or [])
        if refund_chips:
            self._refund_committed_chips(result_events)
        result_events.append(
            GameEvent(
                "table_completed",
                {"reason": reason, "hand_number": self._hand_number},
            )
        )
        return ActionResult(
            ok=False,
            error=ActionValidationError(code=code, message=message),
            events=tuple(result_events),
            state_changed=True,
        )

    def _refund_committed_chips(self, events: list[GameEvent]) -> None:
        for seat in self._seats:
            if seat.committed_this_hand > 0:
                refund = seat.committed_this_hand
                seat.stack += refund
                seat.committed_this_hand = 0
                seat.committed_this_street = 0
                if seat.all_in and seat.stack > 0:
                    seat.all_in = False
                events.append(
                    GameEvent(
                        "chips_refunded",
                        {"seat_id": seat.seat_id, "amount": refund},
                    )
                )

    def _next_action_index_from(self, index: int | None) -> int | None:
        if index is None:
            return None
        current = index
        for _ in range(len(self._seats)):
            current = (current + 1) % len(self._seats)
            seat = self._seats[current]
            if seat.in_hand and not seat.folded and not seat.all_in:
                return current
        return None

    def _next_active_index(self, index: int) -> int:
        current = index
        for _ in range(len(self._seats)):
            current = (current + 1) % len(self._seats)
            if self._seats[current].stack > 0:
                return current
        raise RuntimeError("No active seat found")

    def _next_occupied_index(self, index: int) -> int | None:
        current = index
        for _ in range(len(self._seats)):
            current = (current + 1) % len(self._seats)
            if self._seats[current].in_hand or self._seats[current].stack > 0:
                return current
        return None

    def _active_indices(self) -> list[int]:
        return [index for index, seat in enumerate(self._seats) if seat.in_hand]

    def _invalid(self, code: str, message: str) -> ActionResult:
        return ActionResult(
            ok=False,
            error=ActionValidationError(code=code, message=message),
            state_changed=False,
        )

    def _seat_snapshot(self, seat: _SeatState) -> SeatSnapshot:
        return SeatSnapshot(
            seat_id=seat.seat_id,
            name=seat.name,
            stack=seat.stack,
            contribution=seat.committed_this_hand,
            folded=seat.folded,
            all_in=seat.all_in,
            in_hand=seat.in_hand,
            position=seat.position,
            street_contribution=seat.committed_this_street,
        )

    def _hand_seat_state(self, seat: _SeatState) -> HandSeatState:
        return HandSeatState(
            seat_id=seat.seat_id,
            name=seat.name,
            stack=seat.stack,
            hole_cards=tuple(seat.hole_cards),
            folded=seat.folded,
            all_in=seat.all_in,
            in_hand=seat.in_hand,
            committed_this_street=seat.committed_this_street,
            committed_this_hand=seat.committed_this_hand,
            has_acted_this_round=seat.has_acted_this_round,
            last_action_bet_level=seat.last_action_bet_level,
            position=seat.position,
        )

    def _seat_id_or_none(self, index: int | None) -> str | None:
        if index is None:
            return None
        return self._seats[index].seat_id

    def _index_of_optional_seat(self, seat_id: str | None, *, default: int | None = None) -> int | None:
        if seat_id is None:
            return default
        return self._index_of_seat(seat_id)

    def _index_of_seat(self, seat_id: str) -> int:
        for index, seat in enumerate(self._seats):
            if seat.seat_id == seat_id:
                return index
        raise KeyError(seat_id)

    def _require_seat(self, seat_id: str) -> _SeatState:
        try:
            return self._seat_by_id[seat_id]
        except KeyError as exc:
            raise KeyError(f"Unknown seat id: {seat_id}") from exc
