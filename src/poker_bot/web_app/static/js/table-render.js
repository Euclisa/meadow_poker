import {
  escapeHtml,
  formatChips,
  prettyPhaseLabel,
  prettyStatusLabel,
  renderCard,
  renderCards,
} from "./shared.js";

import {
  compactEventText,
  orderSeatsForDisplay,
  seatPositionStyle,
  seatStatusMeta,
  shortPositionLabel,
} from "./table-utils.js";

export function renderStatusMarkup(snapshot, { flash = "", flashTone = "info", joinName = "", busy = false, actionAmount = "" } = {}) {
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
        flash
          ? `<div class="flash flash--${escapeHtml(flashTone)}">${escapeHtml(flash)}</div>`
          : ""
      }

      ${renderPlayWindow(snapshot, { joinName, busy, actionAmount })}
      ${renderHistoryStrip(snapshot)}
    </main>
  `;
}

function renderPlayWindow(snapshot, uiState) {
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
        ${renderToolbar(snapshot, uiState)}
      </section>
    `;
  }

  return `
    <section class="play-window">
      ${renderTableSurface(snapshot)}
      ${renderToolbar(snapshot, uiState)}
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

function renderToolbar(snapshot, { joinName = "", busy = false, actionAmount = "" } = {}) {
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
                <input id="join-seat-name" name="display_name" type="text" value="${escapeHtml(joinName)}" maxlength="40" placeholder="Your name" required>
              </label>
              <button class="button button--primary" ${busy ? "disabled" : ""} type="submit">Claim seat</button>
            </form>
          `
          : renderToolbarActions(snapshot, pendingDecision, { busy, actionAmount })
      }
    </section>
  `;
}

function renderToolbarActions(snapshot, pendingDecision, { busy = false, actionAmount = "" } = {}) {
  const controls = snapshot.controls;

  if (pendingDecision) {
    return renderActionBar(pendingDecision, { busy, actionAmount });
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
            ? `<button id="start-table-button" class="button button--primary" ${busy ? "disabled" : ""} type="button">Start</button>`
            : ""
        }
        ${
          controls.can_cancel
            ? `<button id="cancel-table-button" class="button button--ghost" ${busy ? "disabled" : ""} type="button">Cancel</button>`
            : ""
        }
        ${
          controls.can_leave
            ? `<button id="leave-table-button" class="button button--ghost" ${busy ? "disabled" : ""} type="button">Leave</button>`
            : ""
        }
      </div>
    </div>
  `;
}

function renderActionBar(pendingDecision, { busy = false, actionAmount = "" } = {}) {
  const rangedAction = pendingDecision.legal_actions.find(
    (action) => action.action_type === "bet" || action.action_type === "raise",
  );
  const defaultAmount =
    actionAmount ||
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
                ${busy ? "disabled" : ""}
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
