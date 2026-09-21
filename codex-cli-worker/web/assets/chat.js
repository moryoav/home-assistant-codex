"use strict";
const $ = (id) => document.getElementById(id);
const labels = {
  queued: "Queued",
  running: "Working",
  waiting_for_input: "Needs your reply",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Stopped",
};
const state = {
  id: null,
  task: null,
  chats: [],
  next: null,
  active: null,
  busy: false,
  loading: false,
  generation: 0,
  drafts: new Map(),
  signature: "",
  listSignature: "",
};
const welcome = $("messages").innerHTML;
let refreshTimer;
let usageTimer;
let usageLoading = false;
function renderUsage(usage = {}) {
  // The worker's percentages already represent quota left, not quota used.
  for (const [id, key] of [
    ["usage-five-hour", "five_hour"],
    ["usage-weekly", "weekly"],
  ]) {
    const raw = usage[`${key}_percent`];
    const percent =
      typeof raw === "number" || (typeof raw === "string" && raw.trim())
        ? Number(raw)
        : NaN;
    const valid = Number.isFinite(percent) && percent >= 0 && percent <= 100;
    $(id).textContent = valid ? `${percent}% left` : "Unavailable";
    const reset = dateLabel(usage[`${key}_reset_at`], true) || usage[`${key}_reset`];
    $(id).title = valid && reset ? `Resets ${reset}` : "";
  }
  $("usage-note").textContent =
    usage.status === "deferred"
      ? "Last known quota; refreshes after the task finishes."
      : usage.status === "ok"
        ? ""
        : "Quota is currently unavailable.";
  $("usage-note").hidden = !$("usage-note").textContent;
}
async function loadUsage() {
  if (usageLoading) return;
  usageLoading = true;
  clearTimeout(usageTimer);
  try {
    if (!document.hidden) {
      const data = await api("status");
      renderUsage(data.codex_usage);
    }
  } catch (_) {
    renderUsage();
  } finally {
    usageLoading = false;
    usageTimer = setTimeout(loadUsage, 60000);
  }
}
async function api(path, body) {
  const response = await fetch(path.replace(/^\//, ""), {
    method: body === undefined ? "GET" : "POST",
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const data = await response.json().catch(() => {
    throw new Error(
      `The worker returned HTTP ${response.status}. Try refreshing.`,
    );
  });
  if (!response.ok || !data.ok)
    throw new Error(
      data.error || `The worker returned HTTP ${response.status}.`,
    );
  return data;
}
function textNode(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text || "";
  if (className) node.className = className;
  return node;
}
function showError(error) {
  $("error").textContent = error ? String(error.message || error) : "";
  $("error").hidden = !error;
}
function dateLabel(value, full = false) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return full
    ? date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
    : date.toLocaleDateString([], { month: "short", day: "numeric" });
}
function sidebar(open) {
  document.body.classList.toggle("sidebar-open", open);
  $("scrim").hidden = !open;
  $("menu").setAttribute("aria-expanded", String(open));
  $("sidebar").inert = matchMedia("(max-width:700px)").matches && !open;
  document.querySelector("main").inert = matchMedia("(max-width:700px)").matches && open;
  if (open) $("new-chat").focus();
}
function resizeInput() {
  $("message").style.height = "auto";
  $("message").style.height = Math.min(180, $("message").scrollHeight) + "px";
}
function controls() {
  const task = state.task;
  const active =
    Boolean(state.active) ||
    Boolean(task && ["queued", "running"].includes(task.status));
  $("send").disabled =
    state.busy ||
    state.loading ||
    active ||
    (state.id !== null && !task?.can_continue) ||
    !$("message").value.trim();
  $("cancel").hidden = !task || !["queued", "running"].includes(task.status);
  $("cancel").disabled = state.busy;
  $("new-chat").disabled = state.busy;
  $("message").readOnly = state.busy;
  $("compose-hint").textContent = state.id
    ? "Continue this conversation with its saved context."
    : "A new chat starts a separate conversation.";
  let notice = "";
  if (state.active && state.active !== state.id)
    notice =
      "Another chat is working. You can send this message when it finishes.";
  else if (active)
    notice =
      "Codex is working. Your response will appear here when it finishes.";
  else if (task && !task.can_continue)
    notice =
      "This chat has no saved session to continue. Start a new chat and include the context you need.";
  $("notice").textContent = notice;
  $("notice").hidden = !notice;
}
function renderList() {
  const signature = JSON.stringify([state.chats, state.id]);
  if (signature === state.listSignature) return;
  state.listSignature = signature;
  const list = $("chat-list");
  const previousFocus = document.activeElement?.dataset.taskId;
  list.replaceChildren();
  for (const chat of state.chats) {
    const row = textNode("button", "", "chat-row");
    row.dataset.taskId = chat.task_id;
    row.setAttribute("aria-current", String(chat.task_id === state.id));
    row.append(textNode("span", chat.title || "Untitled chat", "row-title"));
    row.append(
      textNode(
        "span",
        chat.summary || chat.question || labels[chat.status] || chat.status,
        "row-preview",
      ),
    );
    const meta = textNode("span", "", "row-meta");
    meta.append(
      textNode(
        "span",
        labels[chat.status] || chat.status,
        chat.status === "waiting_for_input" ? "waiting" : "",
      ),
    );
    meta.append(
      textNode("span", dateLabel(chat.updated_at || chat.created_at)),
    );
    row.append(meta);
    row.onclick = () => selectChat(chat.task_id);
    list.append(row);
    if (previousFocus === chat.task_id) row.focus();
  }
  $("list-empty").hidden = state.chats.length > 0;
  $("load-more").hidden = state.next === null;
}
async function loadList(older = false) {
  const count = older ? 20 : Math.max(20, Math.min(500, state.chats.length));
  const offset = older ? state.chats.length : 0;
  const data = await api(
    `tasks?summary=true&order=updated_desc&limit=${count}&offset=${offset}`,
  );
  const merged = new Map(state.chats.map((chat) => [chat.task_id, chat]));
  for (const chat of data.tasks) merged.set(chat.task_id, chat);
  state.chats = [...merged.values()].sort(
    (a, b) =>
      (b.updated_at || b.created_at).localeCompare(
        a.updated_at || a.created_at,
      ) || b.task_id.localeCompare(a.task_id),
  );
  state.next = state.chats.length < data.total ? state.chats.length : null;
  state.active = data.active_task_id;
  renderList();
  controls();
}
function renderTask(force = false) {
  const task = state.task;
  if (!task) return;
  $("chat-title").textContent = task.title || "Untitled chat";
  $("chat-status").textContent =
    `${labels[task.status] || task.status} · ${dateLabel(task.updated_at, true)}`;
  const signature = JSON.stringify([
    task.turns,
    task.history_incomplete,
    task.status,
  ]);
  if (!force && signature === state.signature) {
    controls();
    return;
  }
  state.signature = signature;
  const area = $("messages");
  const nearBottom =
    area.scrollHeight - area.scrollTop - area.clientHeight < 100;
  const oldScroll = area.scrollTop;
  area.replaceChildren();
  if (task.history_incomplete)
    area.append(
      textNode(
        "p",
        "This chat predates conversation history. Available messages and the latest saved response are shown; some earlier responses may be missing.",
        "history-note",
      ),
    );
  for (const turn of task.turns || []) {
    const exchange = textNode("section", "", "exchange");
    if (turn.message)
      exchange.append(textNode("div", turn.message, "message user"));
    const hasAnswer = turn.summary || turn.details || turn.question;
    if (hasAnswer) {
      const answer = textNode("div", "", "answer");
      const heading = textNode("div", "", "answer-heading");
      heading.append(textNode("span", "⌘", "mark"), textNode("span", "Codex"));
      answer.append(heading);
      if (turn.summary) answer.append(textNode("div", turn.summary, "message"));
      if (turn.details && turn.details !== turn.summary)
        answer.append(textNode("div", turn.details, "message details"));
      if (turn.question && turn.question !== turn.summary)
        answer.append(textNode("div", turn.question, "message question"));
      answer.append(
        textNode(
          "div",
          [
            labels[turn.status],
            dateLabel(
              turn.completed_at || turn.updated_at || turn.created_at,
              true,
            ),
          ]
            .filter(Boolean)
            .join(" · "),
          "turn-meta",
        ),
      );
      exchange.append(answer);
    } else if (["queued", "running"].includes(turn.status)) {
      exchange.append(
        textNode(
          "p",
          turn.status === "queued"
            ? "Getting started…"
            : "Working on your request…",
          "pending",
        ),
      );
    }
    area.append(exchange);
  }
  area.scrollTop = force || nearBottom ? area.scrollHeight : oldScroll;
  controls();
}
async function fetchSelected(force = false) {
  const id = state.id;
  const generation = state.generation;
  if (!id) return;
  const data = await api(`tasks/${encodeURIComponent(id)}`);
  if (generation !== state.generation || id !== state.id) return;
  state.task = data.task;
  state.loading = false;
  renderTask(force);
}
async function selectChat(id) {
  if (state.busy) return;
  state.drafts.set(state.id, $("message").value);
  state.id = id;
  state.task = null;
  state.generation++;
  state.signature = "";
  state.loading = Boolean(id);
  $("message").value = state.drafts.get(id) || "";
  resizeInput();
  showError(null);
  sidebar(false);
  renderList();
  controls();
  if (!id) {
    $("chat-title").textContent = "New chat";
    $("chat-status").textContent = "Your Home Assistant, with a little help.";
    $("messages").innerHTML = welcome; // Static, application-owned welcome content only.
    return;
  }
  $("chat-title").textContent =
    state.chats.find((chat) => chat.task_id === id)?.title || "Conversation";
  $("chat-status").textContent = "Loading…";
  $("messages").replaceChildren(
    textNode("p", "Loading conversation…", "muted"),
  );
  try {
    await fetchSelected(true);
  } catch (error) {
    if (state.id === id) showError(error);
  }
}
$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  if ($("send").disabled) return;
  const message = $("message").value.trim();
  const id = state.id;
  state.busy = true;
  controls();
  showError(null);
  try {
    const result = await api(
      id ? `tasks/${encodeURIComponent(id)}/continue` : "tasks",
      id ? { message } : { prompt: message },
    );
    state.drafts.delete(id);
    $("message").value = "";
    state.active = result.task_id;
    state.busy = false;
    await selectChat(result.task_id);
    await loadList();
  } catch (error) {
    showError(error);
    // A failed request may have saved a terminal task; refresh status without retrying the message.
    await Promise.allSettled([loadList(), fetchSelected()]);
  } finally {
    state.busy = false;
    controls();
    resizeInput();
  }
});
$("message").addEventListener("input", () => {
  resizeInput();
  controls();
});
$("message").addEventListener("keydown", (event) => {
  if (
    event.key === "Enter" &&
    !event.shiftKey &&
    !event.isComposing &&
    !matchMedia("(max-width:700px)").matches
  ) {
    event.preventDefault();
    $("composer").requestSubmit();
  }
});
$("messages").addEventListener("click", (event) => {
  const button = event.target.closest("[data-prompt]");
  if (button) {
    $("message").value = button.dataset.prompt;
    resizeInput();
    controls();
    $("message").focus();
  }
});
$("new-chat").onclick = () => selectChat(null);
$("menu").onclick = () => sidebar(true);
$("close-sidebar").onclick = $("scrim").onclick = () => {
  sidebar(false);
  $("menu").focus();
};
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") sidebar(false);
});
matchMedia("(max-width:700px)").addEventListener("change", () =>
  sidebar(false),
);
$("refresh").onclick = async () => {
  loadUsage();
  try {
    await loadList();
    await fetchSelected();
    showError(null);
  } catch (error) {
    showError(error);
  }
};
$("load-more").onclick = async () => {
  $("load-more").disabled = true;
  try {
    await loadList(true);
  } catch (error) {
    showError(error);
  } finally {
    $("load-more").disabled = false;
  }
};
$("cancel").onclick = async () => {
  state.busy = true;
  controls();
  try {
    await api(`tasks/${encodeURIComponent(state.id)}/cancel`, {});
    await fetchSelected();
    await loadList();
  } catch (error) {
    showError(error);
  } finally {
    state.busy = false;
    controls();
  }
};
function setWidth(value) {
  const width = Math.max(
    220,
    Math.min(440, Number(value) || 280, innerWidth - 360),
  );
  document.documentElement.style.setProperty("--sidebar-width", `${width}px`);
  $("divider").setAttribute("aria-valuenow", String(width));
  try {
    localStorage.setItem("codex-sidebar-width", String(width));
  } catch (_) {
    /* Storage may be disabled. */
  }
}
try {
  setWidth(localStorage.getItem("codex-sidebar-width") || 280);
} catch (_) {
  setWidth(280);
}
$("divider").addEventListener("pointerdown", (event) => {
  if (event.button !== 0) return;
  $("divider").setPointerCapture(event.pointerId);
  document.body.classList.add("resizing");
});
$("divider").addEventListener("pointermove", (event) => {
  if ($("divider").hasPointerCapture(event.pointerId)) setWidth(event.clientX);
});
$("divider").addEventListener("pointerup", (event) => {
  $("divider").releasePointerCapture(event.pointerId);
});
$("divider").addEventListener("lostpointercapture", () =>
  document.body.classList.remove("resizing"),
);
$("divider").addEventListener("keydown", (event) => {
  if (["ArrowLeft", "ArrowRight"].includes(event.key)) {
    event.preventDefault();
    setWidth(
      Number($("divider").getAttribute("aria-valuenow")) +
        (event.key === "ArrowLeft" ? -20 : 20),
    );
  }
});
window.addEventListener("resize", () => {
  if (innerWidth > 700) setWidth($("divider").getAttribute("aria-valuenow"));
});
let loginTimer;
async function loadLogin() {
  clearTimeout(loginTimer);
  const { auth } = await api("auth/status");
  $("login-status").textContent =
    auth.message || auth.status || "Not signed in";
  $("login-card").replaceChildren();
  if (auth.user_code)
    $("login-card").append(textNode("p", `Sign-in code: ${auth.user_code}`));
  if (auth.verification_url) {
    const url = new URL(auth.verification_url, location.href);
    if (url.protocol === "https:") {
      const link = textNode("a", "Open ChatGPT sign-in");
      link.href = url.href;
      link.target = "_blank";
      link.rel = "noreferrer";
      $("login-card").append(link);
    }
  }
  if (auth.qr_url && auth.qr_url.startsWith("/local/")) {
    const img = document.createElement("img");
    img.src = auth.qr_url;
    img.alt = "ChatGPT sign-in QR code";
    $("login-card").append(document.createElement("br"), img);
  }
  if (
    $("settings").open &&
    ["waiting_for_user", "starting", "queued"].includes(auth.status)
  )
    loginTimer = setTimeout(() => loadLogin().catch(settingsError), 3000);
}
function settingsError(error) {
  $("settings-result").textContent = String(error.message || error);
}
$("settings-button").onclick = async () => {
  sidebar(false);
  $("settings").showModal();
  $("settings-result").textContent = "";
  $("agents").disabled = true;
  $("save-agents").disabled = true;
  const results = await Promise.allSettled([
    loadLogin(),
    api("agents").then((data) => {
      $("agents").value = data.content || "";
      $("agents").disabled = false;
      $("save-agents").disabled = false;
    }),
  ]);
  for (const result of results)
    if (result.status === "rejected") settingsError(result.reason);
};
$("close-settings").onclick = () => $("settings").close();
$("settings").addEventListener("close", () => clearTimeout(loginTimer));
for (const [id, path] of [
  ["login", "auth/start"],
  ["logout", "auth/logout"],
]) {
  $(id).onclick = async () => {
    $(id).disabled = true;
    try {
      await api(path, {});
      await loadLogin();
      $("settings-result").textContent = "";
    } catch (error) {
      settingsError(error);
    } finally {
      $(id).disabled = false;
    }
  };
}
$("save-agents").onclick = async () => {
  $("save-agents").disabled = true;
  try {
    await api("agents", { content: $("agents").value });
    $("settings-result").textContent = "Instructions saved.";
  } catch (error) {
    settingsError(error);
  } finally {
    $("save-agents").disabled = false;
  }
};
async function refresh() {
  clearTimeout(refreshTimer);
  if (!document.hidden && !state.busy) {
    try {
      await loadList();
      await fetchSelected();
    } catch (error) {
      showError(error);
    }
  }
  refreshTimer = setTimeout(refresh, state.active ? 3000 : 12000);
}
sidebar(false);
controls();
refresh();
loadUsage();
