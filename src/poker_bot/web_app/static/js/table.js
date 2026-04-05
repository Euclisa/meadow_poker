import {
  clearSeatToken,
  copyText,
  loadSeatToken,
  openSnapshotStream,
  requestJson,
  storeSeatToken,
  tableShareUrl,
} from "./shared.js";

import { renderStatusMarkup } from "./table-render.js";
import { morph } from "./morph.js";

const appRoot = document.getElementById("app");
const tableId = document.body.dataset.tableId;

const state = {
  tableId,
  snapshot: null,
  seatToken: loadSeatToken(tableId),
  busy: false,
  coachPending: false,
  flash: "",
  flashTone: "info",
  joinName: "",
  actionAmount: "",
  stream: null,
  coachReply: "",
  coachVisible: false,
  coachAbortController: null,
  coachRequestToken: 0,
  coachTurnKey: "",
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

export function renderTableMarkup(snapshot) {
  return renderStatusMarkup(snapshot, {
    flash: state.flash,
    flashTone: state.flashTone,
    joinName: state.joinName,
    busy: state.busy,
    coachPending: state.coachPending,
    actionAmount: state.actionAmount,
    coachReply: state.coachReply,
    coachVisible: state.coachVisible,
  });
}

let initialized = false;

function render() {
  const html = renderTableMarkup(state.snapshot);
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
    const target = event.target.closest("[id], [data-action-type], [data-preset-amount], .copy-share-button");
    if (!target) return;

    if (target.id === "copy-share-link") {
      if (!state.snapshot) return;
      copyText(tableShareUrl(state.snapshot.controls.share_path)).then(() => {
        setFlash("Share link copied to the clipboard.", "success");
      });
      return;
    }

    if (target.id === "start-table-button") { performTableCommand("start"); return; }
    if (target.id === "cancel-table-button") { performTableCommand("cancel"); return; }
    if (target.id === "leave-table-button") { performTableCommand("leave"); return; }
    if (target.id === "coach-button") { requestCoachTip(); return; }
    if (target.id === "coach-bubble-close") {
      cancelCoachRequest();
      state.coachVisible = false;
      render();
      return;
    }

    if (target.dataset.presetAmount != null) {
      state.actionAmount = target.dataset.presetAmount;
      render();
      return;
    }

    if (target.dataset.actionType) {
      submitAction(target.dataset.actionType);
    }
  });

  appRoot.addEventListener("input", (event) => {
    const target = event.target;
    if (target.id === "join-seat-name") { state.joinName = target.value; }
    if (target.id === "action-amount") { state.actionAmount = target.value; }
  });

  appRoot.addEventListener("submit", (event) => {
    const form = event.target.closest("form");
    if (form?.id === "join-seat-form") {
      onJoinSeat(event);
    }
  });
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
  cancelCoachRequest();
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

async function requestCoachTip() {
  if (!state.snapshot?.controls?.can_request_coach || state.coachPending) {
    return;
  }
  clearFlash();
  cancelCoachRequest();
  state.coachPending = true;
  state.coachReply = "";
  state.coachVisible = false;
  state.coachRequestToken += 1;
  const requestToken = state.coachRequestToken;
  state.coachTurnKey = currentTurnKey(state.snapshot);
  state.coachAbortController = new AbortController();
  render();

  try {
    const payload = await requestJson(`/api/tables/${state.tableId}/coach`, {
      method: "POST",
      body: JSON.stringify({
        seat_token: state.seatToken,
      }),
      signal: state.coachAbortController.signal,
    });
    if (requestToken !== state.coachRequestToken) {
      return;
    }
    if (currentTurnKey(state.snapshot) !== state.coachTurnKey) {
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

function cancelCoachRequest() {
  if (state.coachAbortController) {
    state.coachAbortController.abort();
  }
  state.coachAbortController = null;
  state.coachPending = false;
  state.coachRequestToken += 1;
}

function resolveSubmittedAmount(actionType) {
  if (actionType !== "bet" && actionType !== "raise") {
    return null;
  }

  const amountInput = document.getElementById("action-amount");
  const rawValue = state.actionAmount.trim() || amountInput?.value?.trim() || "";
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
  syncCoachStateAfterSnapshot();
  render();
}

function connectStream() {
  state.stream?.close();
  const tokenQuery = state.seatToken ? `?seat_token=${encodeURIComponent(state.seatToken)}` : "";
  state.stream = openSnapshotStream(`/api/tables/${state.tableId}/stream${tokenQuery}`, {
    onSnapshot(snapshot) {
      state.snapshot = snapshot;
      syncCoachStateAfterSnapshot();
      render();
    },
    onError() {
      window.setTimeout(connectStream, 1500);
    },
  });
}

function currentTurnKey(snapshot) {
  const handNumber = snapshot?.public_table?.hand_number ?? "none";
  const seatId = snapshot?.player_view?.seat_id ?? "none";
  const isTurn = snapshot?.pending_decision ? "turn" : "idle";
  return `${handNumber}:${seatId}:${isTurn}`;
}

function syncCoachStateAfterSnapshot() {
  if (!state.snapshot?.pending_decision) {
    cancelCoachRequest();
  }
}

async function init() {
  await loadState();
  connectStream();
}

init().catch((error) => {
  setFlash(error.message, "error");
});
