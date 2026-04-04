/**
 * Minimal DOM-morph: updates `target` to match `newHtml` without replacing
 * the entire subtree.  Unchanged nodes stay in place so CSS animations,
 * focus, and scroll positions are preserved.
 */
export function morph(target, newHtml) {
  const template = document.createElement("div");
  template.innerHTML = newHtml;
  morphChildren(target, template);
}

function morphChildren(existing, incoming) {
  const oldNodes = Array.from(existing.childNodes);
  const newNodes = Array.from(incoming.childNodes);

  const max = Math.max(oldNodes.length, newNodes.length);
  for (let i = 0; i < max; i++) {
    const oldNode = oldNodes[i];
    const newNode = newNodes[i];

    if (!newNode) {
      existing.removeChild(oldNode);
      continue;
    }

    if (!oldNode) {
      existing.appendChild(newNode.cloneNode(true));
      continue;
    }

    if (oldNode.nodeType !== newNode.nodeType || nodeName(oldNode) !== nodeName(newNode)) {
      existing.replaceChild(newNode.cloneNode(true), oldNode);
      continue;
    }

    if (oldNode.nodeType === Node.TEXT_NODE) {
      if (oldNode.textContent !== newNode.textContent) {
        oldNode.textContent = newNode.textContent;
      }
      continue;
    }

    if (oldNode.nodeType !== Node.ELEMENT_NODE) {
      continue;
    }

    syncAttributes(oldNode, newNode);

    // For inputs/selects, sync the live value property separately so we
    // don't fight with user edits.  Skip if the element is focused.
    if (document.activeElement !== oldNode) {
      if (oldNode.tagName === "INPUT" || oldNode.tagName === "TEXTAREA") {
        if (oldNode.value !== newNode.getAttribute("value")) {
          oldNode.value = newNode.getAttribute("value") ?? "";
        }
      } else if (oldNode.tagName === "SELECT") {
        const desired = newNode.getAttribute("value");
        if (desired != null && oldNode.value !== desired) {
          oldNode.value = desired;
        }
      }
    }

    morphChildren(oldNode, newNode);
  }
}

function syncAttributes(oldEl, newEl) {
  const newAttrs = newEl.attributes;
  const oldAttrs = oldEl.attributes;
  // Skip "selected" on <option> — the <select>.value is the source of truth
  // and syncing the attribute would reset the user's choice.
  const isOption = oldEl.tagName === "OPTION";

  // Add / update
  for (let i = 0; i < newAttrs.length; i++) {
    const { name, value } = newAttrs[i];
    if (isOption && name === "selected") continue;
    if (oldEl.getAttribute(name) !== value) {
      oldEl.setAttribute(name, value);
    }
  }

  // Remove stale
  for (let i = oldAttrs.length - 1; i >= 0; i--) {
    const { name } = oldAttrs[i];
    if (isOption && name === "selected") continue;
    if (!newEl.hasAttribute(name)) {
      oldEl.removeAttribute(name);
    }
  }
}

function nodeName(node) {
  return node.nodeName.toLowerCase();
}
