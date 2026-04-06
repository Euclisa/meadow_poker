const LEAF_SVG = `<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 20A7 7 0 0 1 9.8 6.9C15.5 4.9 17 3.5 19 2c1 2 2 4.5 1 8-1.5 5.5-5 7-9 10z"/><path d="M2 21c0-3 1.85-5.36 5.08-6C9.5 14.52 12 13 13 12"/></svg>`;

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
  seatBetSide,
  seatPositionStyle,
  seatStatusMeta,
  shortPositionLabel,
} from "./table-utils.js";

export function renderStatusMarkup(snapshot, {
  flash = "",
  flashTone = "info",
  joinName = "",
  busy = false,
  coachPending = false,
  actionAmount = "",
  coachReply = "",
  coachVisible = false,
} = {}) {
  if (!snapshot) {
    return `<div class="panel panel--wide loading-card">Loading table...</div>`;
  }
  const replay = snapshot.replay;

  return `
    <main class="app-shell app-shell--table">
      <section class="table-header-card table-header-card--compact">
        <div>
          <p class="eyebrow">${replay?.active ? `Replay · Hand #${escapeHtml(replay.hand_number)}` : `Table ${escapeHtml(snapshot.table_id)}`}</p>
          <h1>${escapeHtml(snapshot.message || "Poker table")}</h1>
        </div>
        <div class="table-header-card__badges">
          <span class="status-pill">${prettyStatusLabel(snapshot.status)}</span>
          <span class="chip chip--soft">${replay?.active ? `step ${replay.current_step + 1}/${replay.total_steps}` : `/${escapeHtml(snapshot.table_id)}`}</span>
          ${replay?.active
            ? `<a class="button button--ghost" href="/table/${escapeHtml(snapshot.table_id)}">Back to table</a>`
            : `<button class="button button--ghost" id="copy-share-link" type="button">Copy share link</button>`}
        </div>
      </section>

      ${
        flash
          ? `<div class="flash flash--${escapeHtml(flashTone)}">${escapeHtml(flash)}</div>`
          : ""
      }

      ${renderPlayWindow(snapshot, {
        joinName,
        busy,
        coachPending,
        actionAmount,
        coachReply,
        coachVisible,
      })}
      ${renderCoachBubble({ coachReply, coachVisible, coachPending })}
      ${renderHistoryStrip(snapshot)}
      ${renderCompletedHands(snapshot)}
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
  const turnTimer = snapshot.turn_timer;
  const orderedSeats = orderSeatsForDisplay(publicTable.seats, playerView?.seat_id ?? null);
  const showdown = snapshot.showdown;
  const revealedSeats = new Map((showdown?.revealed_seats ?? []).map((seat) => [seat.seat_id, seat]));
  const seatAmountBadges = new Map((snapshot.seat_amount_badges ?? []).map((badge) => [badge.seat_id, badge]));

  return `
    <section class="table-surface">
      <div class="table-surface__rail"></div>
      <div class="table-surface__center">
        <div class="board-center">
          <div class="card-row card-row--board">${renderCards(publicTable.board_cards, { large: true })}</div>
          <div class="board-center__stats">
            <span class="chip">Pot ${formatChips(publicTable.pot_total)}</span>
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
            reveal: revealedSeats.get(seat.seat_id) ?? null,
            seatAmountBadge: seatAmountBadges.get(seat.seat_id) ?? null,
            turnTimer,
          }),
        )
        .join("")}
    </section>
  `;
}

function renderSeatPanel({ seat, publicTable, playerView, seatCount, displayIndex, reveal, seatAmountBadge, turnTimer }) {
  const isViewer = seat.is_viewer && playerView;
  const style = seatPositionStyle(displayIndex, seatCount);
  const status = seatStatusMeta(seat, publicTable.acting_seat_id);
  const positionLabel = seat.position ? shortPositionLabel(seat.position) : "";
  const positionTitle = seat.position ? prettyPhaseLabel(seat.position) : "";
  const amountBadgeSide = seatBetSide(displayIndex, seatCount);
  const cardsMarkup = reveal
    ? renderCards(reveal.hole_cards)
    : isViewer
      ? renderCards(playerView.hole_cards)
      : seat.in_hand && !seat.folded
        ? `${renderCard("xx", { hidden: true })}${renderCard("xx", { hidden: true })}`
        : "";
  const amountBadgeMarkup = renderSeatAmountBadge(seatAmountBadge, { side: amountBadgeSide });
  const timerMarkup = renderTurnTimerBar(turnTimer, seat.seat_id);

  return `
    <article
      class="table-seat${seat.is_viewer ? " table-seat--viewer" : ""}${seat.seat_id === publicTable.acting_seat_id ? " table-seat--acting" : ""}${seat.folded ? " table-seat--folded" : ""}${reveal ? " table-seat--revealed" : ""}"
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
      ${timerMarkup}
      ${amountBadgeMarkup}
    </article>
  `;
}

function renderSeatAmountBadge(seatAmountBadge, { side }) {
  if (!seatAmountBadge || seatAmountBadge.amount == null || seatAmountBadge.amount <= 0) {
    return "";
  }
  return `
    <div class="table-seat__bet table-seat__bet--${side}">
      <span class="chip-icon" aria-hidden="true"></span>
      <span class="table-seat__bet-amount">${formatChips(seatAmountBadge.amount)}</span>
    </div>
  `;
}

function renderTurnTimerBar(turnTimer, seatId) {
  if (!turnTimer?.enabled || turnTimer.seat_id !== seatId || turnTimer.duration_ms == null || turnTimer.deadline_epoch_ms == null) {
    return "";
  }
  return `
    <div
      class="table-seat__timer"
      data-turn-timer
      data-deadline-epoch-ms="${escapeHtml(turnTimer.deadline_epoch_ms)}"
      data-duration-ms="${escapeHtml(turnTimer.duration_ms)}">
      <span class="table-seat__timer-fill"></span>
    </div>
  `;
}

function renderToolbar(
  snapshot,
  {
    joinName = "",
    busy = false,
    coachPending = false,
    actionAmount = "",
    coachReply = "",
    coachVisible = false,
  } = {},
) {
  if (snapshot.replay?.active) {
    return renderReplayToolbar(snapshot, { coachPending, coachReply, coachVisible });
  }
  const controls = snapshot.controls;
  const summary = snapshot.config_summary;
  const pendingDecision = snapshot.pending_decision;

  // Join form is a separate state — no need for the dock
  if (controls.can_join) {
    return `
      <section class="panel panel--toolbar">
        <div class="toolbar__summary">
          <span class="chip">${summary.claimed_web_seats}/${summary.web_seats} web</span>
          <span class="chip chip--soft">${summary.llm_seats} bots</span>
          <span class="chip chip--soft">${formatChips(summary.small_blind)} / ${formatChips(summary.big_blind)}</span>
          <span class="chip chip--soft">Stack ${formatChips(summary.starting_stack)}</span>
          <span class="chip chip--soft">${summary.stack_depth} BB</span>
        </div>
        <form id="join-seat-form" class="toolbar__join">
          <label class="field toolbar__field">
            <span>Display name</span>
            <input id="join-seat-name" name="display_name" type="text" value="${escapeHtml(joinName)}" maxlength="40" placeholder="Your name" required>
          </label>
          <button class="button button--primary" ${busy ? "disabled" : ""} type="submit">Claim seat</button>
        </form>
      </section>
    `;
  }

  // Always render both idle + active layers; crossfade via CSS class
  const isActing = !!pendingDecision;
  const rangedAction = pendingDecision?.legal_actions?.find(
    (a) => a.action_type === "bet" || a.action_type === "raise",
  );
  const defaultAmount =
    actionAmount ||
    (rangedAction && rangedAction.min_amount != null ? String(rangedAction.min_amount) : "");

  return `
    <section class="action-dock${isActing ? " action-dock--active" : ""}">
      <div class="action-dock__stage">
        <div class="action-dock__idle">
          <span class="action-dock__message">
            ${controls.is_joined
              ? "Waiting for your turn\u2026"
              : escapeHtml(controls.join_disabled_reason || "Use your seat token to rejoin.")}
          </span>
          <div class="action-dock__side">
            ${controls.can_start ? `<button id="start-table-button" class="button button--primary" ${busy ? "disabled" : ""} type="button">Start</button>` : ""}
            ${controls.can_cancel ? `<button id="cancel-table-button" class="button button--ghost" ${busy ? "disabled" : ""} type="button">Cancel</button>` : ""}
            ${controls.can_leave ? `<button id="leave-table-button" class="button button--ghost" ${busy ? "disabled" : ""} type="button">Leave</button>` : ""}
          </div>
        </div>

        <div class="action-dock__live">
          <span class="action-dock__prompt">
            ${pendingDecision
              ? (pendingDecision.to_call > 0
                  ? `To call <strong>${formatChips(pendingDecision.to_call)}</strong>`
                  : "Check or bet")
              : ""}
          </span>
          ${pendingDecision?.validation_error
            ? `<span class="action-dock__error">${escapeHtml(pendingDecision.validation_error.message)}</span>`
            : ""}
          ${rangedAction ? `
            <input class="action-dock__amount" id="action-amount" type="number"
              value="${escapeHtml(defaultAmount)}"
              min="${rangedAction.min_amount}" max="${rangedAction.max_amount}" step="1"
              placeholder="${escapeHtml(prettyPhaseLabel(rangedAction.action_type))} amt">
            <div class="bet-presets">
              ${[["⅓", 1/3], ["½", 1/2], ["¾", 3/4], ["Pot", 1]].map(([label, frac]) => {
                const raw = Math.round(snapshot.public_table.pot_total * frac);
                const clamped = Math.min(rangedAction.max_amount, Math.max(rangedAction.min_amount, raw));
                return `<button class="bet-preset" type="button" data-preset-amount="${clamped}">${label}</button>`;
              }).join("")}
              <button class="bet-preset bet-preset--allin" type="button" data-preset-amount="${rangedAction.max_amount}">All-in</button>
            </div>
          ` : ""}
          <div class="action-dock__buttons">
            ${controls.can_request_coach
              ? `
                  <button
                    class="button button--coach"
                    id="coach-button"
                    type="button"
                    title="Ask the coach for a tip"
                    ${coachPending ? "disabled" : ""}>
                    ${coachPending ? "..." : LEAF_SVG}
                  </button>
                `
              : ""}
            ${pendingDecision
              ? pendingDecision.legal_actions.map((action) => `
                  <button class="button ${action.action_type === "fold" ? "button--fold" : action.action_type === "check" || action.action_type === "call" ? "button--call" : "button--raise"}"
                    data-action-type="${escapeHtml(action.action_type)}"
                    type="button" ${busy ? "disabled" : ""}>
                    ${escapeHtml(prettyPhaseLabel(action.action_type))}${action.action_type === "call" && pendingDecision.to_call > 0 ? ` ${formatChips(pendingDecision.to_call)}` : ""}
                  </button>
                `).join("")
              : ""}
          </div>
        </div>
      </div>
    </section>
  `;
}

function renderReplayToolbar(snapshot, { coachPending = false, coachReply = "", coachVisible = false } = {}) {
  const replay = snapshot.replay;
  return `
    <section class="panel panel--toolbar replay-dock">
      <div class="replay-dock__summary">
        <span class="chip">Hand ${replay.hand_number}</span>
        <span class="chip chip--soft">Step ${replay.current_step + 1}/${replay.total_steps}</span>
        <span class="chip chip--soft">${formatChips(snapshot.config_summary.small_blind)} / ${formatChips(snapshot.config_summary.big_blind)}</span>
      </div>
      <div class="replay-dock__nav">
        <div class="replay-dock__buttons">
          <button class="button button--ghost" id="replay-first-step" type="button" ${replay.can_step_backward ? "" : "disabled"}>&laquo;</button>
          <button class="button button--ghost" id="replay-prev-step" type="button" ${replay.can_step_backward ? "" : "disabled"}>&larr;</button>
          <button class="button button--ghost" id="replay-next-step" type="button" ${replay.can_step_forward ? "" : "disabled"}>&rarr;</button>
          <button class="button button--ghost" id="replay-last-step" type="button" ${replay.can_step_forward ? "" : "disabled"}>&raquo;</button>
        </div>
      </div>
      <div class="replay-dock__analysis">
        <button
          class="button button--coach"
          id="coach-button"
          type="button"
          title="${snapshot.controls.can_request_coach ? "Analyze this replay spot" : "Replay analysis unavailable at this step"}"
          ${!snapshot.controls.can_request_coach || coachPending ? "disabled" : ""}>
          ${coachPending ? "..." : LEAF_SVG}
        </button>
      </div>
    </section>
  `;
}

function renderCoachBubble({
  coachReply = "",
  coachVisible = false,
  coachPending = false,
  label = "Coach tip",
} = {}) {
  if (!coachVisible && !coachPending) {
    return "";
  }
  return `
    <aside class="coach-bubble${coachPending ? " coach-bubble--pending" : ""}">
      <button class="coach-bubble__close" id="coach-bubble-close" type="button" aria-label="Close coach tip">×</button>
      <div class="coach-bubble__label">${escapeHtml(label)}</div>
      <div class="coach-bubble__text">
        ${coachPending ? "Looking at the spot..." : escapeHtml(coachReply)}
      </div>
    </aside>
  `;
}

function renderHistoryStrip(snapshot) {
  const events = (snapshot.recent_events ?? []).slice(-12).reverse();
  return `
    <section class="panel panel--history">
      <span class="history-strip__label">${snapshot.replay?.active ? "Replay timeline" : "Recent"}</span>
      <div class="history-strip__list">
        ${
          events.length === 0
            ? `<span class="history-line history-line--empty">No recent action yet.</span>`
            : events
                .map(
                  (event) => `
                    <span class="history-line history-line--${escapeHtml(event.kind || "state")}">
                      ${compactEventText(event.text)}
                    </span>
                  `,
                )
                .join("")
        }
      </div>
    </section>
  `;
}

function renderCompletedHands(snapshot) {
  const hands = snapshot.completed_hands ?? [];
  return `
    <section class="panel panel--history panel--completed-hands">
      <span class="history-strip__label">Completed hands</span>
      <div class="completed-hands__list">
        ${
          hands.length === 0
            ? `<span class="completed-hands__item completed-hands__item--empty">Completed hands will appear here.</span>`
            : hands
                .map(
                  (hand) => `
                    <a class="completed-hands__item" href="${escapeHtml(hand.replay_path)}" target="_blank" rel="noreferrer">
                      <span>Hand #${escapeHtml(hand.hand_number)}</span>
                      <span class="chip chip--soft">${hand.ended_in_showdown ? "showdown" : "no showdown"}</span>
                    </a>
                  `,
                )
                .join("")
        }
      </div>
    </section>
  `;
}
