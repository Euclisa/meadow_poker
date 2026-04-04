const SUITS = {
  s: { symbol: "♠", color: "is-spade" },
  h: { symbol: "♥", color: "is-heart" },
  d: { symbol: "♦", color: "is-diamond" },
  c: { symbol: "♣", color: "is-club" },
};

export function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

export function prettyPhaseLabel(phase) {
  return String(phase ?? "")
    .split("_")
    .filter(Boolean)
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join(" ");
}

export function prettyStatusLabel(status) {
  return prettyPhaseLabel(status);
}

export function formatChips(value) {
  const numeric = Number(value ?? 0);
  return Number.isFinite(numeric) ? numeric.toLocaleString("en-US") : "0";
}

export function cardMeta(card) {
  if (!card || card.length < 2) {
    return { rank: "?", suit: "?", symbol: "?", colorClass: "is-spade" };
  }
  const suitKey = card.slice(-1).toLowerCase();
  const suit = SUITS[suitKey] ?? { symbol: card.slice(-1), color: "is-spade" };
  return {
    rank: card.slice(0, -1),
    suit: suitKey,
    symbol: suit.symbol,
    colorClass: suit.color,
  };
}

export function renderCard(card, { hidden = false, large = false } = {}) {
  if (hidden) {
    return `
      <span class="playing-card playing-card--back${large ? " playing-card--large" : ""}" aria-hidden="true">
        <span class="playing-card__backdrop"></span>
      </span>
    `;
  }

  const meta = cardMeta(card);
  return `
    <span class="playing-card ${meta.colorClass}${large ? " playing-card--large" : ""}" aria-label="${escapeHtml(card)}">
      <span class="playing-card__corner">
        <span class="playing-card__rank">${escapeHtml(meta.rank)}</span>
        <span class="playing-card__suit">${meta.symbol}</span>
      </span>
      <span class="playing-card__pip">${meta.symbol}</span>
    </span>
  `;
}

export function renderCards(cards, options = {}) {
  if (!cards || cards.length === 0) {
    return `<span class="cards-empty">No cards yet</span>`;
  }
  return cards.map((card) => renderCard(card, options)).join("");
}

export function renderEventFeed(events) {
  if (!events || events.length === 0) {
    return `<div class="feed-empty">The table is quiet for now.</div>`;
  }

  return events
    .map(
      (event) => `
        <article class="feed-item feed-item--${escapeHtml(event.kind || "state")}">
          <span class="feed-item__text">${escapeHtml(event.text)}</span>
        </article>
      `,
    )
    .join("");
}

export async function requestJson(url, options = {}) {
  const response = await fetch(url, {
    headers: {
      "Content-Type": "application/json",
      ...(options.headers ?? {}),
    },
    ...options,
  });

  const contentType = response.headers.get("content-type") || "";
  const isJson = contentType.includes("application/json");
  const payload = isJson ? await response.json() : null;
  if (!response.ok) {
    const message =
      payload?.error?.message ||
      payload?.message ||
      (await response.text().catch(() => "")) ||
      `Request failed with status ${response.status}`;
    throw new Error(message);
  }
  return payload;
}

export function openSnapshotStream(url, { onSnapshot, onError }) {
  const source = new EventSource(url);
  source.addEventListener("snapshot", (event) => {
    onSnapshot(JSON.parse(event.data));
  });
  source.addEventListener("error", () => {
    if (onError) {
      onError();
    }
  });
  return source;
}

export async function copyText(value) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }

  const input = document.createElement("textarea");
  input.value = value;
  document.body.append(input);
  input.select();
  document.execCommand("copy");
  input.remove();
}

export function seatTokenStorageKey(tableId) {
  return `poker_bot:web_table:${tableId}:seat_token`;
}

export function loadSeatToken(tableId) {
  return localStorage.getItem(seatTokenStorageKey(tableId));
}

export function storeSeatToken(tableId, token) {
  if (!token) {
    return;
  }
  localStorage.setItem(seatTokenStorageKey(tableId), token);
}

export function clearSeatToken(tableId) {
  localStorage.removeItem(seatTokenStorageKey(tableId));
}

export function tableShareUrl(sharePath) {
  return new URL(sharePath, window.location.origin).toString();
}
