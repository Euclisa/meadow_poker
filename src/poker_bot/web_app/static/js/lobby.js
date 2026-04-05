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
    small_blind: 50,
    big_blind: 100,
    starting_stack: 2000,
  };
  const playerOptions = Array.from({ length: Math.max(0, defaults.max_players - 1) }, (_, index) => index + 2)
    .map(
      (count) => `
        <option value="${count}" ${count === 4 ? "selected" : ""}>${count} seats</option>
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
            <span>Blinds</span>
            <strong>${formatChips(defaults.small_blind)} / ${formatChips(defaults.big_blind)}</strong>
          </div>
          <div class="stat-pill">
            <span>Stack</span>
            <strong>${formatChips(defaults.starting_stack)}</strong>
          </div>
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
              <select id="create-llm-seats" name="llm_seat_count">
                <option value="0">0 bots</option>
                <option value="1" selected>1 bot</option>
                <option value="2">2 bots</option>
                <option value="3">3 bots</option>
                <option value="4">4 bots</option>
                <option value="5">5 bots</option>
              </select>
            </label>
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

  syncLlmSeatOptions();
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
    if (target.id === "join-name") { state.joinName = target.value; }
    if (target.id === "join-code") { state.joinCode = target.value.trim(); }
  });

  appRoot.addEventListener("change", (event) => {
    if (event.target.id === "create-total-seats") {
      syncLlmSeatOptions();
    }
  });

  appRoot.addEventListener("submit", (event) => {
    const form = event.target.closest("form");
    if (form?.id === "create-table-form") { onCreateTable(event); }
    if (form?.id === "join-table-form") { onJoinTable(event); }
  });
}

function syncLlmSeatOptions() {
  const totalSeatsInput = document.getElementById("create-total-seats");
  const llmSeatsInput = document.getElementById("create-llm-seats");
  if (!totalSeatsInput || !llmSeatsInput) {
    return;
  }
  const totalSeats = Number(totalSeatsInput.value || 2);
  const previous = Number(llmSeatsInput.value || 0);
  llmSeatsInput.innerHTML = Array.from({ length: Math.max(0, totalSeats) }, (_, index) => index)
    .filter((count) => count < totalSeats)
    .map((count) => `<option value="${count}">${count} bot${count === 1 ? "" : "s"}</option>`)
    .join("");
  llmSeatsInput.value = String(Math.min(previous, Math.max(0, totalSeats - 1)));
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
