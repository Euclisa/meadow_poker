import { loadSeatToken, requestJson } from "./shared.js";
import { renderStatusMarkup } from "./table-render.js";
import { morph } from "./morph.js";

const appRoot = document.getElementById("app");
const tableId = document.body.dataset.tableId;
const handNumber = document.body.dataset.handNumber;

const state = {
  snapshot: null,
  seatToken: loadSeatToken(tableId),
  step: 0,
  busy: false,
  flash: "",
  flashTone: "info",
};

let initialized = false;

function render() {
  const html = renderStatusMarkup(state.snapshot, {
    flash: state.flash,
    flashTone: state.flashTone,
  });
  if (initialized) {
    morph(appRoot, html);
  } else {
    appRoot.innerHTML = html;
    initialized = true;
    bindDelegatedEvents();
  }
}

function setFlash(message, tone = "info") {
  state.flash = message;
  state.flashTone = tone;
  render();
}

function clearFlash() {
  state.flash = "";
  render();
}

function bindDelegatedEvents() {
  appRoot.addEventListener("click", (event) => {
    const target = event.target.closest("[id]");
    if (!target || !state.snapshot?.replay) {
      return;
    }
    if (target.id === "replay-first-step") {
      loadReplayStep(0);
      return;
    }
    if (target.id === "replay-prev-step") {
      loadReplayStep(Math.max(0, state.step - 1));
      return;
    }
    if (target.id === "replay-next-step") {
      loadReplayStep(Math.min(state.snapshot.replay.total_steps - 1, state.step + 1));
      return;
    }
    if (target.id === "replay-last-step") {
      loadReplayStep(state.snapshot.replay.total_steps - 1);
    }
  });
}

async function loadReplayStep(step) {
  clearFlash();
  state.busy = true;
  render();
  try {
    const query = new URLSearchParams();
    if (state.seatToken) {
      query.set("seat_token", state.seatToken);
    }
    query.set("step", String(step));
    state.snapshot = await requestJson(`/api/tables/${tableId}/replay/${handNumber}?${query.toString()}`);
    state.step = state.snapshot.replay?.current_step ?? step;
    state.busy = false;
    render();
  } catch (error) {
    state.busy = false;
    setFlash(error.message, "error");
  }
}

loadReplayStep(0);
