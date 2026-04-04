import { prettyPhaseLabel } from "./shared.js";

export function shortPositionLabel(position) {
  const aliases = {
    dealer: "BTN",
    small_blind: "SB",
    big_blind: "BB",
    under_the_gun: "UTG",
    middle_position: "MP",
    cutoff: "CO",
  };

  if (aliases[position]) {
    return aliases[position];
  }

  return prettyPhaseLabel(position)
    .split(/\s+/)
    .map((part) => part.slice(0, 1))
    .join("")
    .slice(0, 3)
    .toUpperCase();
}

export function orderSeatsForDisplay(seats, viewerSeatId) {
  const orderedSeats = [...seats];
  if (!viewerSeatId) {
    return orderedSeats;
  }
  const viewerIndex = orderedSeats.findIndex((seat) => seat.seat_id === viewerSeatId);
  if (viewerIndex === -1) {
    return orderedSeats;
  }
  return [...orderedSeats.slice(viewerIndex), ...orderedSeats.slice(0, viewerIndex)];
}

export function seatPositionStyle(displayIndex, seatCount) {
  const radiusX = seatCount <= 3 ? 35 : seatCount <= 5 ? 37.5 : 39.5;
  const radiusY = seatCount <= 3 ? 32 : 34;
  let angleDegrees = 90;

  if (displayIndex > 0) {
    if (seatCount === 2) {
      angleDegrees = 270;
    } else {
      const arcStart = 205;
      const arcEnd = 335;
      const slotCount = Math.max(seatCount - 1, 1);
      const ratio = slotCount === 1 ? 0.5 : (displayIndex - 1) / (slotCount - 1);
      angleDegrees = arcStart + (arcEnd - arcStart) * ratio;
    }
  }

  const angle = (angleDegrees * Math.PI) / 180;
  const x = 50 + Math.cos(angle) * radiusX;
  const y = 50 + Math.sin(angle) * radiusY;
  return `--seat-x:${x.toFixed(2)}%; --seat-y:${y.toFixed(2)}%;`;
}

export function seatStatusMeta(seat, actingSeatId) {
  if (seat.folded) {
    return { label: "Fold", tone: "fold" };
  }
  if (seat.all_in) {
    return { label: "All-in", tone: "allin" };
  }
  if (!seat.in_hand) {
    return { label: "Wait", tone: "wait" };
  }
  if (seat.seat_id === actingSeatId) {
    return { label: "Turn", tone: "turn" };
  }
  return null;
}

export function compactEventText(text) {
  return String(text || "").replace(/\s+/g, " ").trim();
}
