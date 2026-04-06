import {
  copyText,
  escapeHtml,
  formatChips,
  openSnapshotStream,
  prettyStatusLabel,
  requestJson,
  storeSeatToken,
  tableShareUrl,
} from "./shared.js";

import { morph } from "./morph.js";

const appRoot = document.getElementById("app");

const state = {
  lobby: null,
  createName: "",
  createTotalSeats: "",
  createLlmSeats: "",
  createBigBlind: "",
  createStackDepth: "",
  createTurnTimeout: "",
  joinName: "",
  joinCode: "",
  busy: false,
  flash: "",
  flashTone: "info",
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

export function renderLobbyMarkup(model) {
  const tables = model.lobby?.tables ?? [];
  const defaults = model.lobby?.defaults ?? {
    max_players: 6,
    big_blind: 100,
    small_blind: 50,
    starting_stack: 2000,
    stack_depth: 20,
    big_blind_presets: [20, 50, 100, 200, 500],
    stack_depth_presets: [20, 40, 100, 200],
    turn_timeout_seconds: null,
  };
  const selectedTotalSeats = Number(model.createTotalSeats || Math.min(4, defaults.max_players));
  const selectedLlmSeats = Math.min(
    Number(model.createLlmSeats || 1),
    Math.max(0, selectedTotalSeats - 1),
  );
  const selectedBigBlind = Number(model.createBigBlind || defaults.big_blind);
  const selectedStackDepth = Number(model.createStackDepth || defaults.stack_depth);
  const selectedTurnTimeout = model.createTurnTimeout === ""
    ? defaults.turn_timeout_seconds
    : Number(model.createTurnTimeout);
  const selectedSmallBlind = Math.max(1, Math.floor(selectedBigBlind / 2));
  const selectedStartingStack = selectedBigBlind * selectedStackDepth;
  const turnTimeoutLabel = selectedTurnTimeout && Number.isFinite(selectedTurnTimeout)
    ? `${selectedTurnTimeout}s`
    : "Off";

  const playerOptions = Array.from({ length: Math.max(0, defaults.max_players - 1) }, (_, index) => index + 2)
    .map(
      (count) => `
        <option value="${count}" ${count === selectedTotalSeats ? "selected" : ""}>${count} seats</option>
      `,
    )
    .join("");
  const llmOptions = Array.from({ length: Math.max(0, selectedTotalSeats) }, (_, index) => index)
    .filter((count) => count < selectedTotalSeats)
    .map(
      (count) => `
        <option value="${count}" ${count === selectedLlmSeats ? "selected" : ""}>${count} bot${count === 1 ? "" : "s"}</option>
      `,
    )
    .join("");
  const bigBlindOptions = defaults.big_blind_presets
    .map(
      (amount) => `
        <option value="${amount}" ${amount === selectedBigBlind ? "selected" : ""}>${formatChips(amount)}</option>
      `,
    )
    .join("");
  const stackDepthOptions = defaults.stack_depth_presets
    .map(
      (depth) => `
        <option value="${depth}" ${depth === selectedStackDepth ? "selected" : ""}>${depth} BB</option>
      `,
    )
    .join("");

  return `
    <main class="app-shell app-shell--lobby">
      <section class="hero-card hero-card--meadow">
        <div class="hero-card__copy">
          <p class="eyebrow">Browser Lobby</p>
          <h1>Pastel felt. Live tables.</h1>
          <p class="hero-card__lede">
            Create a table, share the link, and play right in the browser.
          </p>
        </div>
        <div class="hero-card__stats">
          <div class="stat-pill">
            <span>Open tables</span>
            <strong>${tables.length}</strong>
          </div>
        </div>
      </section>

      ${
        model.flash
          ? `<div class="flash flash--${escapeHtml(model.flashTone)}">${escapeHtml(model.flash)}</div>`
          : ""
      }

      <section class="lobby-grid">
        <article class="panel panel--raised">
          <div class="panel__header">
            <p class="eyebrow">Create Table</p>
            <h2>Spin up a new meadow table</h2>
          </div>
          <form id="create-table-form" class="stack-form">
            <label class="field">
              <span>Display name</span>
              <input id="create-name" name="display_name" type="text" value="${escapeHtml(model.createName)}" maxlength="40" placeholder="Alice" required>
            </label>
            <label class="field">
              <span>Total seats</span>
              <select id="create-total-seats" name="total_seats">${playerOptions}</select>
            </label>
            <label class="field">
              <span>LLM seats</span>
              <select id="create-llm-seats" name="llm_seat_count">${llmOptions}</select>
            </label>
            <section class="settings-card">
              <div class="settings-card__header">
                <div>
                  <p class="eyebrow">Game Settings</p>
                  <h3>Choose the pace</h3>
                </div>
                <span class="chip chip--soft">${formatChips(selectedSmallBlind)} / ${formatChips(selectedBigBlind)}</span>
              </div>
              <div class="settings-card__grid">
                <label class="field">
                  <span>Big blind</span>
                  <select id="create-big-blind" name="big_blind">${bigBlindOptions}</select>
                </label>
                <label class="field">
                  <span>Starting stack</span>
                  <select id="create-stack-depth" name="stack_depth">${stackDepthOptions}</select>
                </label>
                <label class="field">
                  <span>Turn timer</span>
                  <input id="create-turn-timeout" name="turn_timeout_seconds" type="number" min="1" step="1" value="${escapeHtml(model.createTurnTimeout)}" placeholder="Off">
                </label>
              </div>
              <div class="settings-card__summary">
                <div class="settings-stat">
                  <span>Blinds</span>
                  <strong>${formatChips(selectedSmallBlind)} / ${formatChips(selectedBigBlind)}</strong>
                </div>
                <div class="settings-stat">
                  <span>Stack</span>
                  <strong>${formatChips(selectedStartingStack)}</strong>
                </div>
                <div class="settings-stat">
                  <span>Depth</span>
                  <strong>${selectedStackDepth} BB</strong>
                </div>
                <div class="settings-stat">
                  <span>Turn timer</span>
                  <strong>${escapeHtml(turnTimeoutLabel)}</strong>
                </div>
              </div>
            </section>
            <button class="button button--primary" ${model.busy ? "disabled" : ""} type="submit">Create table</button>
          </form>
        </article>

        <article class="panel panel--glass">
          <div class="panel__header">
            <p class="eyebrow">Join by Code</p>
            <h2>Jump straight to a shared table</h2>
          </div>
          <form id="join-table-form" class="stack-form">
            <label class="field">
              <span>Display name</span>
              <input id="join-name" name="display_name" type="text" value="${escapeHtml(model.joinName)}" maxlength="40" placeholder="Bob" required>
            </label>
            <label class="field">
              <span>Table code</span>
              <input id="join-code" name="table_code" type="text" value="${escapeHtml(model.joinCode)}" maxlength="12" placeholder="e3f9ab" required>
            </label>
            <button class="button button--secondary" ${model.busy ? "disabled" : ""} type="submit">Join table</button>
          </form>
        </article>
      </section>

      <section class="panel panel--wide">
        <div class="panel__header panel__header--row">
          <div>
            <p class="eyebrow">Open Tables</p>
            <h2>Tables waiting in the meadow</h2>
          </div>
          <span class="status-pill">${tables.length} waiting</span>
        </div>
        <div class="table-list">
          ${
            tables.length === 0
              ? `<div class="empty-state">No tables are waiting right now. Create one and share the link.</div>`
              : tables
                  .map(
                    (table) => `
                      <article class="table-card">
                        <div class="table-card__head">
                          <div>
                            <h3>Table ${escapeHtml(table.table_id)}</h3>
                            <p>${escapeHtml(table.status_message || "Waiting for players.")}</p>
                          </div>
                          <span class="status-pill">${prettyStatusLabel(table.status)}</span>
                        </div>
                        <dl class="table-card__meta">
                          <div><dt>Seats</dt><dd>${table.claimed_web_seats}/${table.web_seats} web</dd></div>
                          <div><dt>Bots</dt><dd>${table.llm_seats}</dd></div>
                          <div><dt>Blinds</dt><dd>${formatChips(table.small_blind)} / ${formatChips(table.big_blind)}</dd></div>
                          <div><dt>Stack</dt><dd>${formatChips(table.starting_stack)} (${table.stack_depth} BB)</dd></div>
                          <div><dt>Turn timer</dt><dd>${table.turn_timeout_seconds ? `${escapeHtml(String(table.turn_timeout_seconds))}s` : "Off"}</dd></div>
                          <div><dt>Players</dt><dd>${table.waiting_players.map((player) => escapeHtml(player.display_name)).join(", ")}</dd></div>
                        </dl>
                        <div class="table-card__actions">
                          <a class="button button--ghost" href="${escapeHtml(table.share_path)}">Open table</a>
                          <button class="button button--secondary copy-share-button" data-share-path="${escapeHtml(table.share_path)}" type="button">Copy link</button>
                        </div>
                      </article>
                    `,
                  )
                  .join("")
          }
        </div>
      </section>
    </main>
  `;
}

let initialized = false;

function render() {
  const html = renderLobbyMarkup(state);
  if (initialized) {
    morph(appRoot, html);
  } else {
    appRoot.innerHTML = html;
    initialized = true;
    bindDelegatedEvents();
  }
}

function bindDelegatedEvents() {
  appRoot.addEventListener("click", (event) => {
    const copyBtn = event.target.closest(".copy-share-button");
    if (copyBtn) {
      const sharePath = copyBtn.dataset.sharePath;
      copyText(tableShareUrl(sharePath)).then(() => {
        setFlash("Share link copied to the clipboard.", "success");
      });
    }
  });

  appRoot.addEventListener("input", (event) => {
    const target = event.target;
    if (target.id === "create-name") { state.createName = target.value; }
    if (target.id === "create-turn-timeout") { state.createTurnTimeout = target.value; }
    if (target.id === "join-name") { state.joinName = target.value; }
    if (target.id === "join-code") { state.joinCode = target.value.trim(); }
    render();
  });

  appRoot.addEventListener("change", (event) => {
    if (event.target.id === "create-total-seats") {
      state.createTotalSeats = event.target.value;
      const maxBots = Math.max(0, Number(event.target.value || 2) - 1);
      if (Number(state.createLlmSeats || 1) > maxBots) {
        state.createLlmSeats = String(maxBots);
      }
      render();
      return;
    }
    if (event.target.id === "create-llm-seats") { state.createLlmSeats = event.target.value; }
    if (event.target.id === "create-big-blind") { state.createBigBlind = event.target.value; }
    if (event.target.id === "create-stack-depth") { state.createStackDepth = event.target.value; }
    render();
  });

  appRoot.addEventListener("submit", (event) => {
    const form = event.target.closest("form");
    if (form?.id === "create-table-form") { onCreateTable(event); }
    if (form?.id === "join-table-form") { onJoinTable(event); }
  });
}

async function onCreateTable(event) {
  event.preventDefault();
  const form = new FormData(event.target.closest("form"));
  clearFlash();
  state.busy = true;
  render();

  try {
    const payload = await requestJson("/api/tables", {
      method: "POST",
      body: JSON.stringify({
        display_name: form.get("display_name"),
        total_seats: Number(form.get("total_seats")),
        llm_seat_count: Number(form.get("llm_seat_count")),
        big_blind: Number(form.get("big_blind")),
        stack_depth: Number(form.get("stack_depth")),
        turn_timeout_seconds: form.get("turn_timeout_seconds") ? Number(form.get("turn_timeout_seconds")) : null,
      }),
    });
    storeSeatToken(payload.table_id, payload.seat_token);
    window.location.assign(`/table/${payload.table_id}`);
  } catch (error) {
    setFlash(error.message, "error");
    state.busy = false;
    render();
  }
}

async function onJoinTable(event) {
  event.preventDefault();
  const form = new FormData(event.target.closest("form"));
  clearFlash();
  state.busy = true;
  render();

  try {
    const tableId = String(form.get("table_code") || "").trim();
    const payload = await requestJson(`/api/tables/${tableId}/join`, {
      method: "POST",
      body: JSON.stringify({
        display_name: form.get("display_name"),
      }),
    });
    storeSeatToken(payload.table_id, payload.seat_token);
    window.location.assign(`/table/${payload.table_id}`);
  } catch (error) {
    setFlash(error.message, "error");
    state.busy = false;
    render();
  }
}

async function loadLobby() {
  state.lobby = await requestJson("/api/lobby");
  render();
}

function connectLobbyStream() {
  state.stream?.close();
  state.stream = openSnapshotStream("/api/lobby/stream", {
    onSnapshot(snapshot) {
      state.lobby = snapshot;
      render();
    },
    onError() {
      window.setTimeout(connectLobbyStream, 1500);
    },
  });
}

async function init() {
  await loadLobby();
  connectLobbyStream();
}

init().catch((error) => {
  state.flash = error.message;
  state.flashTone = "error";
  render();
});
