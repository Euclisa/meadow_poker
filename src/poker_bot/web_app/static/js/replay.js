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
  coachPending: false,
  coachReply: "",
  coachVisible: false,
  coachAbortController: null,
  coachRequestToken: 0,
  coachStepKey: "",
  flash: "",
  flashTone: "info",
};

let initialized = false;

function render() {
  const html = renderStatusMarkup(state.snapshot, {
    flash: state.flash,
    flashTone: state.flashTone,
    coachPending: state.coachPending,
    coachReply: state.coachReply,
    coachVisible: state.coachVisible,
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
      return;
    }
    if (target.id === "coach-button") {
      requestReplayCoach();
      return;
    }
    if (target.id === "coach-bubble-close") {
      cancelReplayCoachRequest();
      state.coachVisible = false;
      render();
    }
  });
}

async function loadReplayStep(step) {
  clearFlash();
  cancelReplayCoachRequest();
  state.coachVisible = false;
  state.coachReply = "";
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

async function requestReplayCoach() {
  if (!state.snapshot?.controls?.can_request_coach || state.coachPending) {
    return;
  }
  clearFlash();
  cancelReplayCoachRequest();
  state.coachPending = true;
  state.coachReply = "";
  state.coachVisible = false;
  state.coachRequestToken += 1;
  const requestToken = state.coachRequestToken;
  state.coachStepKey = currentReplayStepKey(state.snapshot);
  state.coachAbortController = new AbortController();
  render();

  try {
    const payload = await requestJson(`/api/tables/${tableId}/replay/${handNumber}/coach`, {
      method: "POST",
      body: JSON.stringify({
        seat_token: state.seatToken,
        step: state.step,
      }),
      signal: state.coachAbortController.signal,
    });
    if (requestToken !== state.coachRequestToken) {
      return;
    }
    if (currentReplayStepKey(state.snapshot) !== state.coachStepKey) {
      return;
    }
    state.coachReply = payload.reply ?? "";
    state.coachVisible = Boolean(state.coachReply);
    state.coachPending = false;
    state.coachAbortController = null;
    render();
  } catch (error) {
    if (error.name === "AbortError") {
      return;
    }
    state.coachPending = false;
    state.coachAbortController = null;
    setFlash(error.message, "error");
  }
}

function cancelReplayCoachRequest() {
  if (state.coachAbortController) {
    state.coachAbortController.abort();
  }
  state.coachAbortController = null;
  state.coachPending = false;
  state.coachRequestToken += 1;
}

function currentReplayStepKey(snapshot) {
  const replayHandNumber = snapshot?.replay?.hand_number ?? handNumber;
  const step = snapshot?.replay?.current_step ?? state.step;
  return `${replayHandNumber}:${step}`;
}

loadReplayStep(0);
