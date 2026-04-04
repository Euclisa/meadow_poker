import {
  clearSeatToken,
  copyText,
  escapeHtml,
  formatChips,
  loadSeatToken,
  openSnapshotStream,
  prettyPhaseLabel,
  prettyStatusLabel,
  renderCard,
  renderCards,
  requestJson,
  storeSeatToken,
  tableShareUrl,
} from "./shared.js";

const appRoot = document.getElementById("app");
const tableId = document.body.dataset.tableId;

const state = {
  tableId,
  snapshot: null,
  seatToken: loadSeatToken(tableId),
  busy: false,
  flash: "",
  flashTone: "info",
  joinName: "",
  actionAmount: "",
  stream: null,
};

function setFlash(message, tone = "info") {
  state.flash = message;
  state.flashTone = tone;
  render();
}

function clearFlash() {
  state.flash = "";
  render();
}

function renderStatusMarkup(snapshot) {
  if (!snapshot) {
    return `<div class="panel panel--wide loading-card">Loading table...</div>`;
  }

  return `
    <main class="app-shell app-shell--table">
      <section class="table-header-card table-header-card--compact">
        <div>
          <p class="eyebrow">Table ${escapeHtml(snapshot.table_id)}</p>
          <h1>${escapeHtml(snapshot.message || "Poker table")}</h1>
        </div>
        <div class="table-header-card__badges">
          <span class="status-pill">${prettyStatusLabel(snapshot.status)}</span>
          <span class="chip chip--soft">/${escapeHtml(snapshot.table_id)}</span>
          <button class="button button--ghost" id="copy-share-link" type="button">Copy share link</button>
        </div>
      </section>

      ${
        state.flash
          ? `<div class="flash flash--${escapeHtml(state.flashTone)}">${escapeHtml(state.flash)}</div>`
          : ""
      }

      ${renderPlayWindow(snapshot)}
      ${renderHistoryStrip(snapshot)}
    </main>
  `;
}

function renderPlayWindow(snapshot) {
  const publicTable = snapshot.public_table;

  if (!publicTable) {
    return `
      <section class="play-window play-window--waiting">
        <section class="panel panel--waiting">
          <div class="panel__header panel__header--row">
            <div>
              <p class="eyebrow">Waiting Room</p>
              <h2>Seats claimed</h2>
            </div>
            <span class="chip">${snapshot.waiting_players.length}/${snapshot.config_summary.web_seats} web</span>
          </div>
          <div class="waiting-grid">
            ${snapshot.waiting_players
              .map(
                (player) => `
                  <article class="waiting-seat">
                    <span class="waiting-seat__badge">${escapeHtml(player.seat_id.replace("web_", "Seat "))}</span>
                    <strong>${escapeHtml(player.display_name)}</strong>
                    ${player.is_creator ? '<span class="chip chip--accent">creator</span>' : ""}
                  </article>
                `,
              )
              .join("")}
            ${Array.from({
              length: Math.max(0, snapshot.config_summary.web_seats - snapshot.waiting_players.length),
            })
              .map(
                () => `
                  <article class="waiting-seat waiting-seat--empty">
                    <span class="waiting-seat__badge">Open seat</span>
                    <strong>Waiting for a player</strong>
                  </article>
                `,
              )
              .join("")}
          </div>
        </section>
        ${renderToolbar(snapshot)}
      </section>
    `;
  }

  return `
    <section class="play-window">
      ${renderTableSurface(snapshot)}
      ${renderToolbar(snapshot)}
    </section>
  `;
}

function renderTableSurface(snapshot) {
  const publicTable = snapshot.public_table;
  const playerView = snapshot.player_view;
  const orderedSeats = orderSeatsForDisplay(publicTable.seats, playerView?.seat_id ?? null);

  return `
    <section class="table-surface">
      <div class="table-surface__rail"></div>
      <div class="table-surface__center">
        <div class="board-center">
          <div class="board-center__headline">
            <p class="eyebrow">Common Cards</p>
            <span class="chip chip--soft">${prettyPhaseLabel(publicTable.phase)}</span>
          </div>
          <div class="card-row card-row--board">${renderCards(publicTable.board_cards, { large: true })}</div>
          <div class="board-center__stats">
            <span class="chip">Pot ${formatChips(publicTable.pot_total)}</span>
            <span class="chip chip--soft">Bet ${formatChips(publicTable.current_bet)}</span>
            <span class="chip chip--soft">Hand ${publicTable.hand_number}</span>
          </div>
        </div>
      </div>
      ${orderedSeats
        .map((seat, index) =>
          renderSeatPanel({
            seat,
            publicTable,
            playerView,
            seatCount: orderedSeats.length,
            displayIndex: index,
          }),
        )
        .join("")}
    </section>
  `;
}

function renderSeatPanel({ seat, publicTable, playerView, seatCount, displayIndex }) {
  const isViewer = seat.is_viewer && playerView;
  const style = seatPositionStyle(displayIndex, seatCount);
  const status = seatStatusMeta(seat, publicTable.acting_seat_id);
  const positionLabel = seat.position ? shortPositionLabel(seat.position) : "";
  const positionTitle = seat.position ? prettyPhaseLabel(seat.position) : "";
  const cardsMarkup = isViewer
    ? renderCards(playerView.hole_cards)
    : seat.in_hand && !seat.folded
      ? `${renderCard("xx", { hidden: true })}${renderCard("xx", { hidden: true })}`
      : "";

  return `
    <article
      class="table-seat${seat.is_viewer ? " table-seat--viewer" : ""}${seat.seat_id === publicTable.acting_seat_id ? " table-seat--acting" : ""}${seat.folded ? " table-seat--folded" : ""}"
      style="${style}"
    >
      ${cardsMarkup ? `<div class="table-seat__cards">${cardsMarkup}</div>` : ""}
      <div class="table-seat__info">
        <div class="table-seat__top">
          <span class="table-seat__name">${escapeHtml(seat.name)}</span>
          <div class="table-seat__badges">
            ${
              positionLabel
                ? `<span class="seat-badge" title="${escapeHtml(positionTitle)}">${escapeHtml(positionLabel)}</span>`
                : ""
            }
            ${seat.is_viewer ? '<span class="seat-badge seat-badge--you">You</span>' : ""}
          </div>
        </div>
        <div class="table-seat__meta">
          <span class="table-seat__stack">${formatChips(seat.stack)}$</span>
          ${
            status
              ? `<span class="table-seat__state table-seat__state--${status.tone}">${escapeHtml(status.label)}</span>`
              : ""
          }
        </div>
      </div>
    </article>
  `;
}

function renderToolbar(snapshot) {
  const controls = snapshot.controls;
  const summary = snapshot.config_summary;
  const pendingDecision = snapshot.pending_decision;

  return `
    <section class="panel panel--toolbar">
      <div class="toolbar__summary">
        <span class="chip">${summary.claimed_web_seats}/${summary.web_seats} web</span>
        <span class="chip chip--soft">${summary.llm_seats} bots</span>
        <span class="chip chip--soft">${formatChips(summary.small_blind)} / ${formatChips(summary.big_blind)}</span>
        <span class="chip chip--soft">Stack ${formatChips(summary.starting_stack)}</span>
      </div>

      ${
        controls.can_join
          ? `
            <form id="join-seat-form" class="toolbar__join">
              <label class="field toolbar__field">
                <span>Display name</span>
                <input id="join-seat-name" name="display_name" type="text" value="${escapeHtml(state.joinName)}" maxlength="40" placeholder="Your name" required>
              </label>
              <button class="button button--primary" ${state.busy ? "disabled" : ""} type="submit">Claim seat</button>
            </form>
          `
          : renderToolbarActions(snapshot, pendingDecision)
      }
    </section>
  `;
}

function renderToolbarActions(snapshot, pendingDecision) {
  const controls = snapshot.controls;

  if (pendingDecision) {
    return renderActionBar(pendingDecision);
  }

  return `
    <div class="toolbar__actions">
      <div class="toolbar__message">
        ${
          controls.is_joined
            ? "Your action controls will appear here when the turn reaches you."
            : escapeHtml(controls.join_disabled_reason || "Use your seat token to rejoin this table.")
        }
      </div>
      <div class="toolbar__buttons">
        ${
          controls.can_start
            ? `<button id="start-table-button" class="button button--primary" ${state.busy ? "disabled" : ""} type="button">Start</button>`
            : ""
        }
        ${
          controls.can_cancel
            ? `<button id="cancel-table-button" class="button button--ghost" ${state.busy ? "disabled" : ""} type="button">Cancel</button>`
            : ""
        }
        ${
          controls.can_leave
            ? `<button id="leave-table-button" class="button button--ghost" ${state.busy ? "disabled" : ""} type="button">Leave</button>`
            : ""
        }
      </div>
    </div>
  `;
}

function renderActionBar(pendingDecision) {
  const rangedAction = pendingDecision.legal_actions.find(
    (action) => action.action_type === "bet" || action.action_type === "raise",
  );
  const defaultAmount =
    state.actionAmount ||
    (rangedAction && rangedAction.min_amount != null ? String(rangedAction.min_amount) : "");

  return `
    <div class="toolbar__action-bar">
      <div class="toolbar__message">
        ${pendingDecision.to_call > 0 ? `Call ${formatChips(pendingDecision.to_call)}` : "Check or make a bet."}
        ${
          pendingDecision.validation_error
            ? `<span class="toolbar__error">${escapeHtml(pendingDecision.validation_error.message)}</span>`
            : ""
        }
      </div>
      ${
        rangedAction
          ? `
            <label class="field toolbar__field toolbar__field--amount">
              <span>${escapeHtml(rangedAction.action_type)} amount</span>
              <input id="action-amount" type="number" value="${escapeHtml(defaultAmount)}" min="${rangedAction.min_amount}" max="${rangedAction.max_amount}" step="1">
            </label>
          `
          : ""
      }
      <div class="toolbar__buttons toolbar__buttons--actions">
        ${pendingDecision.legal_actions
          .map(
            (action) => `
              <button
                class="button ${action.action_type === "fold" ? "button--ghost" : "button--primary"}"
                data-action-type="${escapeHtml(action.action_type)}"
                type="button"
                ${state.busy ? "disabled" : ""}
              >
                ${escapeHtml(prettyPhaseLabel(action.action_type))}
              </button>
            `,
          )
          .join("")}
      </div>
    </div>
  `;
}

function renderHistoryStrip(snapshot) {
  const events = (snapshot.recent_events ?? []).slice(-8).reverse();
  return `
    <section class="panel panel--history">
      <div class="history-strip">
        <span class="history-strip__label">Recent</span>
        <div class="history-strip__list">
          ${
            events.length === 0
              ? `<span class="history-line history-line--empty">No recent action yet.</span>`
              : events
                  .map(
                    (event) => `
                      <span class="history-line history-line--${escapeHtml(event.kind || "state")}">
                        ${escapeHtml(compactEventText(event.text))}
                      </span>
                    `,
                  )
                  .join("")
          }
        </div>
      </div>
    </section>
  `;
}

function compactEventText(text) {
  return String(text || "").replace(/\s+/g, " ").trim();
}

function shortPositionLabel(position) {
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

function orderSeatsForDisplay(seats, viewerSeatId) {
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

function seatPositionStyle(displayIndex, seatCount) {
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

function seatStatusMeta(seat, actingSeatId) {
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

export function renderTableMarkup(snapshot) {
  return renderStatusMarkup(snapshot);
}

function render() {
  appRoot.innerHTML = renderTableMarkup(state.snapshot);

  document.getElementById("copy-share-link")?.addEventListener("click", async () => {
    if (!state.snapshot) {
      return;
    }
    await copyText(tableShareUrl(state.snapshot.controls.share_path));
    setFlash("Share link copied to the clipboard.", "success");
  });

  document.getElementById("join-seat-name")?.addEventListener("input", (event) => {
    state.joinName = event.currentTarget.value;
  });

  document.getElementById("action-amount")?.addEventListener("input", (event) => {
    state.actionAmount = event.currentTarget.value;
  });

  document.getElementById("join-seat-form")?.addEventListener("submit", onJoinSeat);
  document.getElementById("start-table-button")?.addEventListener("click", () => performTableCommand("start"));
  document.getElementById("cancel-table-button")?.addEventListener("click", () => performTableCommand("cancel"));
  document.getElementById("leave-table-button")?.addEventListener("click", () => performTableCommand("leave"));

  for (const button of document.querySelectorAll("[data-action-type]")) {
    button.addEventListener("click", () => submitAction(button.dataset.actionType));
  }
}

async function onJoinSeat(event) {
  event.preventDefault();
  clearFlash();
  state.busy = true;
  render();

  try {
    const payload = await requestJson(`/api/tables/${state.tableId}/join`, {
      method: "POST",
      body: JSON.stringify({
        display_name: state.joinName,
      }),
    });
    state.snapshot = payload.snapshot;
    state.seatToken = payload.seat_token;
    storeSeatToken(state.tableId, payload.seat_token);
    state.busy = false;
    render();
    connectStream();
  } catch (error) {
    state.busy = false;
    setFlash(error.message, "error");
  }
}

async function performTableCommand(command) {
  clearFlash();
  state.busy = true;
  render();

  try {
    const payload = await requestJson(`/api/tables/${state.tableId}/${command}`, {
      method: "POST",
      body: JSON.stringify({
        seat_token: state.seatToken,
      }),
    });
    if (command === "leave") {
      clearSeatToken(state.tableId);
      state.seatToken = null;
    }
    state.snapshot = payload.snapshot;
    state.busy = false;
    render();
    connectStream();
  } catch (error) {
    state.busy = false;
    setFlash(error.message, "error");
  }
}

async function submitAction(actionType) {
  clearFlash();
  state.busy = true;
  render();

  try {
    const amount = resolveSubmittedAmount(actionType);
    const payload = await requestJson(`/api/tables/${state.tableId}/action`, {
      method: "POST",
      body: JSON.stringify({
        seat_token: state.seatToken,
        action_type: actionType,
        amount,
      }),
    });
    if (payload.snapshot) {
      state.snapshot = payload.snapshot;
    }
    state.actionAmount = "";
    state.busy = false;
    render();
  } catch (error) {
    state.busy = false;
    setFlash(error.message, "error");
  }
}

function resolveSubmittedAmount(actionType) {
  if (actionType !== "bet" && actionType !== "raise") {
    return null;
  }

  const amountInput = document.getElementById("action-amount");
  const rawValue = amountInput?.value?.trim() ?? state.actionAmount.trim();
  if (rawValue) {
    state.actionAmount = rawValue;
    return Number(rawValue);
  }

  const rangedAction = state.snapshot?.pending_decision?.legal_actions?.find(
    (action) => action.action_type === actionType,
  );
  if (rangedAction?.min_amount != null) {
    return Number(rangedAction.min_amount);
  }

  return null;
}

async function loadState() {
  const tokenQuery = state.seatToken ? `?seat_token=${encodeURIComponent(state.seatToken)}` : "";
  try {
    state.snapshot = await requestJson(`/api/tables/${state.tableId}/state${tokenQuery}`);
  } catch (error) {
    if (state.seatToken) {
      clearSeatToken(state.tableId);
      state.seatToken = null;
      state.snapshot = await requestJson(`/api/tables/${state.tableId}/state`);
    } else {
      throw error;
    }
  }
  render();
}

function connectStream() {
  state.stream?.close();
  const tokenQuery = state.seatToken ? `?seat_token=${encodeURIComponent(state.seatToken)}` : "";
  state.stream = openSnapshotStream(`/api/tables/${state.tableId}/stream${tokenQuery}`, {
    onSnapshot(snapshot) {
      state.snapshot = snapshot;
      render();
    },
    onError() {
      window.setTimeout(connectStream, 1500);
    },
  });
}

async function init() {
  await loadState();
  connectStream();
}

init().catch((error) => {
  setFlash(error.message, "error");
});
