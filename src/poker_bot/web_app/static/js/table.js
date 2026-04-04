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

export function renderTableMarkup(snapshot) {
  return renderStatusMarkup(snapshot, {
    flash: state.flash,
    flashTone: state.flashTone,
    joinName: state.joinName,
    busy: state.busy,
    actionAmount: state.actionAmount,
  });
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
