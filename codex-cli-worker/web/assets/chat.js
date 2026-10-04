"use strict";
/** Return the page element with the given id. */
const $ = (id) => document.getElementById(id);
const labels = {
  queued: "Starting",
  in_queue: "In queue",
  running: "Working",
  waiting_for_input: "Needs your reply",
  completed: "Completed",
  failed: "Failed",
  cancelled: "Stopped",
};
// Mirrors the worker's limits for images attached to a message.
const UPLOAD_MAX_BYTES = 10 * 1024 * 1024;
const UPLOADS_PER_MESSAGE = 6;
const UPLOAD_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
const UPLOAD_MAX_EDGE = 2048;
const state = {
  id: null,
  task: null,
  chats: [],
  queue: [],
  edit: null,
  files: [],
  draftFiles: new Map(),
  next: null,
  active: null,
  busy: false,
  loading: false,
  generation: 0,
  drafts: new Map(),
  signature: "",
  listSignature: "",
  catalog: null,
  chatSettings: new Map(),
  settingsBusy: false,
  activity: null,
};
const effortLabels = {
  low: "Low",
  medium: "Medium",
  high: "High",
  xhigh: "Extra High",
  max: "Max",
  ultra: "Ultra",
  minimal: "Minimal",
};
const effortDescriptions = {
  low: "Fast responses with lighter reasoning.",
  medium: "Balances speed and reasoning depth.",
  high: "More reasoning for complex problems.",
  xhigh: "Extra reasoning for demanding tasks.",
  max: "Maximum reasoning for the hardest problems.",
  ultra: "Maximum reasoning with automatic task delegation.",
};
/**
 * Return the model and reasoning level selected for the open chat. A null
 * value means the chat follows the add-on setting.
 */
function selectedSettings() {
  return (
    state.chatSettings.get(state.id) || { model: null, reasoning_effort: null }
  );
}
/** The models a chat can run on, with the add-on's own model when it is not one of the usual choices. */
function chatModels() {
  const { models = [], default_model: id, default_efforts: efforts } =
    state.catalog || {};
  return !id || models.some((model) => model.id === id)
    ? models
    : [{ id, label: id, efforts }, ...models];
}
/**
 * Return the entry of the model a chat runs on: the one it selected, or else
 * the add-on's own model. Undefined when that model is not among the choices.
 */
function selectedModel(settings = selectedSettings()) {
  return chatModels().find(
    (model) => model.id === (settings.model || state.catalog?.default_model),
  );
}
/**
 * Return the reasoning levels a chat can choose from: those of its model, or
 * those of the add-on's model, or none before the chat options are loaded.
 */
function selectedEfforts(settings = selectedSettings()) {
  return (
    selectedModel(settings)?.efforts || state.catalog?.default_efforts || []
  );
}
/**
 * Return the reasoning level that applies to a chat: its own selection, or
 * else the add-on default. Falls back to medium when neither is known or the
 * chat's model does not offer that level.
 */
function effectiveEffort(settings = selectedSettings()) {
  const effort =
    settings.reasoning_effort ||
    state.catalog?.defaults.reasoning_effort ||
    "medium";
  return selectedModel(settings) && !selectedEfforts(settings).includes(effort)
    ? "medium"
    : effort;
}
/**
 * Close the model and reasoning popovers. With focus, the button of the one
 * that was open gets the focus back.
 */
function closePicker(focus = false) {
  for (const name of ["model", "effort"]) {
    if (focus && !$(name + "-popover").hidden) $(name + "-button").focus();
    $(name + "-popover").hidden = true;
    $(name + "-button").setAttribute("aria-expanded", "false");
  }
}
/**
 * Fill the reasoning popover for a level, by default the one that applies to
 * the open chat: its name and description, the model name, and the slider
 * with one dot per level.
 */
function renderEffort(effort = effectiveEffort()) {
  const efforts = selectedEfforts();
  const index = Math.max(0, efforts.indexOf(effort));
  $("effort-title").textContent = effortLabels[effort] || effort;
  $("effort-model").textContent = modelLabel();
  $("effort-slider").max = Math.max(0, efforts.length - 1);
  $("effort-slider").value = index;
  $("effort-slider").setAttribute(
    "aria-valuetext",
    effortLabels[effort] || effort,
  );
  $("effort-slider").style.setProperty(
    "--effort-fill",
    `${(index / Math.max(1, efforts.length - 1)) * 100}%`,
  );
  $("effort-dots").replaceChildren(...efforts.map(() => textNode("span", "")));
  $("effort-description").textContent =
    effortDescriptions[effort] || "Using the add-on default reasoning level.";
}
/** The name of the model a chat runs on, whether it selected one or follows the add-on setting. */
function modelLabel(settings = selectedSettings()) {
  return selectedModel(settings)?.label || settings.model || "Model";
}
/**
 * Show the open chat's model and reasoning level on the picker buttons. The
 * picker is disabled and closed until the chat options are loaded, while the
 * chat loads or its task is queued or running, and while a message, a stop
 * request, or a selection is being sent.
 */
function renderPicker() {
  const settings = selectedSettings();
  const effort = effectiveEffort(settings);
  $("model-button").textContent = modelLabel(settings);
  $("effort-label").textContent = effortLabels[effort] || effort;
  $("effort-button").title = settings.reasoning_effort
    ? "Select reasoning level"
    : "Using add-on default reasoning";
  const disabled =
    !state.catalog ||
    state.loading ||
    state.busy ||
    state.settingsBusy ||
    Boolean(state.task && ["queued", "running"].includes(state.task.status)) ||
    // A waiting message keeps the settings it was sent with.
    Boolean(state.task?.queued_message);
  for (const id of [
    "model-button",
    "effort-button",
    "effort-slider",
    "effort-reset",
  ])
    $(id).disabled = disabled;
  if (disabled) closePicker();
  if (!$("effort-popover").hidden) renderEffort();
}
/**
 * Store a model and reasoning selection for the open chat. A saved chat sends
 * it to the worker and keeps what the worker returns; a new chat only holds it
 * in state until its first message is sent. Errors show above the composer.
 */
async function saveSelection(settings) {
  const id = state.id;
  state.settingsBusy = true;
  controls();
  showError(null);
  try {
    if (id) {
      const data = await api(`tasks/${encodeURIComponent(id)}/settings`, {
        chat_settings: settings,
      });
      settings = data.chat_settings;
      if (state.task) state.task.chat_settings = settings;
    }
    state.chatSettings.set(id, settings);
  } catch (error) {
    showError(error);
  } finally {
    state.settingsBusy = false;
    controls();
  }
}
/**
 * Open the model or reasoning popover, or close it when it is already open.
 * The model popover gets one button per model that saves the choice; the
 * reasoning popover is filled and its slider focused.
 */
function openPicker(name) {
  const wasOpen = !$(name + "-popover").hidden;
  closePicker();
  if (wasOpen) return;
  $(name + "-popover").hidden = false;
  $(name + "-button").setAttribute("aria-expanded", "true");
  if (name === "model") {
    const options = $("model-options");
    options.replaceChildren();
    for (const model of chatModels()) {
      const button = textNode("button", "", "model-option");
      button.type = "button";
      const selected = model.id === selectedModel()?.id;
      button.setAttribute("aria-pressed", String(selected));
      const title = textNode("span", "", "model-option-title");
      title.append(
        textNode("span", model.label),
        textNode("span", selected ? "✓" : "", "model-check"),
      );
      button.append(title);
      button.onclick = async () => {
        // The add-on's own model is saved as no selection, so the chat keeps following the add-on setting.
        const settings = {
          ...selectedSettings(),
          model: model.id === state.catalog.default_model ? null : model.id,
        };
        if (
          settings.reasoning_effort &&
          !selectedEfforts(settings).includes(settings.reasoning_effort)
        )
          settings.reasoning_effort = null;
        closePicker(true);
        await saveSelection(settings);
        $("model-button").focus();
      };
      options.append(button);
    }
    // A chat can hold a model that is no longer in the list; then none is marked.
    (
      options.querySelector('[aria-pressed="true"]') ||
      options.firstElementChild
    )?.focus();
  } else {
    renderEffort();
    $("effort-slider").focus();
  }
}
$("model-button").onclick = () => openPicker("model");
$("effort-button").onclick = () => openPicker("effort");
$("effort-slider").addEventListener("input", () =>
  renderEffort(selectedEfforts()[Number($("effort-slider").value)]),
);
$("effort-slider").addEventListener("change", async () => {
  const effort = selectedEfforts()[Number($("effort-slider").value)];
  closePicker(true);
  await saveSelection({ ...selectedSettings(), reasoning_effort: effort });
  $("effort-button").focus();
});
$("effort-reset").onclick = async () => {
  closePicker(true);
  await saveSelection({ ...selectedSettings(), reasoning_effort: null });
  $("effort-button").focus();
};
document.addEventListener("pointerdown", (event) => {
  if (!$("chat-picker").contains(event.target)) closePicker();
});
$("chat-picker").addEventListener("focusout", (event) => {
  if (event.relatedTarget && !$("chat-picker").contains(event.relatedTarget))
    closePicker();
});
$("chat-picker").addEventListener("keydown", (event) => {
  if (event.key === "Escape") {
    event.preventDefault();
    closePicker(true);
  }
  if (
    !$("model-popover").hidden &&
    ["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)
  ) {
    event.preventDefault();
    const options = [...$("model-options").children];
    const index = options.indexOf(document.activeElement);
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? options.length - 1
          : (index + (event.key === "ArrowDown" ? 1 : -1) + options.length) %
            options.length;
    options[next].focus();
  }
});
let optionsRequest = 0;
/**
 * Load the models, defaults, and settings the UI needs into state.catalog and
 * update the controls. With quiet, a failed request shows no error.
 */
async function loadChatOptions(quiet = false) {
  // A slow answer must not replace the catalog of a request made after it.
  const request = ++optionsRequest;
  try {
    const catalog = await api("chat-options");
    if (request === optionsRequest) state.catalog = catalog;
  } catch (error) {
    if (!quiet) showError(error);
  }
  controls();
}
const welcome = $("messages").innerHTML;
let refreshTimer;
let activityTimer = null;
let usageTimer;
let usageLoading = false;
/**
 * Show the quota left for the five-hour and weekly limits as a figure and a
 * bar, with the reset time as a tooltip. A limit without a valid percentage
 * reads Unavailable, and a note says when the quota is stale or missing.
 */
function renderUsage(usage) {
  // An account can lack either limit, and the worker may have no quota data at all.
  usage = usage || {};
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
    // The bar is as long as the quota left, turns red below 5%, and is an outline without a value.
    const bar = $(`${id}-bar`);
    bar.style.width = valid ? `${percent}%` : "0";
    bar.parentElement.classList.toggle("low", valid && percent < 5);
    bar.parentElement.classList.toggle("unknown", !valid);
    const reset =
      dateLabel(usage[`${key}_reset_at`], true) || usage[`${key}_reset`];
    $(id).title = bar.parentElement.title =
      valid && reset ? `Resets ${reset}` : "";
  }
  $("usage-note").textContent =
    usage.status === "deferred"
      ? "Last known quota; refreshes after the task finishes."
      : usage.status === "ok"
        ? ""
        : "Quota is currently unavailable.";
  $("usage-note").hidden = !$("usage-note").textContent;
}
/**
 * Fetch the worker status, show its quota, and reload the chat options, then
 * schedule the next check in a minute. Nothing is fetched while the page is
 * hidden, and a failed check shows the quota as unavailable.
 */
async function loadUsage() {
  if (usageLoading) return;
  usageLoading = true;
  clearTimeout(usageTimer);
  try {
    if (!document.hidden) {
      const data = await api("status");
      renderUsage(data.codex_usage);
      // The quota check also tells the worker which model Codex picks when the add-on names none.
      // Not awaited: the quota shown must not depend on it.
      if (state.catalog) loadChatOptions(true);
    }
  } catch (_) {
    renderUsage();
  } finally {
    usageLoading = false;
    usageTimer = setTimeout(loadUsage, 60000);
  }
}
/** Send a JSON request to the worker API and return the parsed response. */
async function api(path, body, method) {
  const response = await fetch(path.replace(/^\//, ""), {
    method: method || (body === undefined ? "GET" : "POST"),
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  const data = await response.json().catch(() => null);
  if (!data)
    throw workerError(
      `The worker returned HTTP ${response.status}. Try refreshing.`,
      response.status,
    );
  if (!response.ok || !data.ok)
    throw workerError(
      data.error || `The worker returned HTTP ${response.status}.`,
      response.status,
    );
  return data;
}
/** Build an error that keeps the HTTP status so callers can tell 404 apart. */
function workerError(message, status) {
  const error = new Error(message);
  error.status = status;
  return error;
}
/** Create an element with the given text and optional class name. */
function textNode(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text || "";
  if (className) node.className = className;
  return node;
}
/** Show or clear the error line above the composer. */
function showError(error) {
  $("error").textContent = error ? String(error.message || error) : "";
  $("error").hidden = !error;
}
/**
 * Format a timestamp in the browser's locale as a day and month, or as a full
 * date and time when full is set. Returns "" for a value that is not a date.
 */
function dateLabel(value, full = false) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "";
  return full
    ? date.toLocaleString([], { dateStyle: "medium", timeStyle: "short" })
    : date.toLocaleDateString([], { month: "short", day: "numeric" });
}
/**
 * Open or close the sidebar, which slides over the chat on narrow screens.
 * There, whichever of the two is out of reach is made inert. Closing also
 * closes the chat actions menu; opening moves focus to New chat.
 */
function sidebar(open) {
  if (!open) closeChatMenu();
  document.body.classList.toggle("sidebar-open", open);
  $("scrim").hidden = !open;
  $("menu").setAttribute("aria-expanded", String(open));
  $("sidebar").inert = matchMedia("(max-width:700px)").matches && !open;
  document.querySelector("main").inert =
    matchMedia("(max-width:700px)").matches && open;
  if (open) $("new-chat").focus();
}
/** Fit the height of the message box to its text, up to 180 pixels. */
function resizeInput() {
  $("message").style.height = "auto";
  $("message").style.height = Math.min(180, $("message").scrollHeight) + "px";
}
/** Say when a waiting message will start, from its place in the queue. */
function queueNote(entry) {
  const ahead = entry.position - 1;
  return ahead > 0
    ? `Starts on its own after the running chat and the ${ahead === 1 ? "message" : `${ahead} messages`} ahead of it in the queue.`
    : "Next in line. Starts on its own when the running chat finishes.";
}
/**
 * Update the controls around the open chat from state: whether Send, Stop
 * task, New chat, and Attach can be used, whether sending means queueing, the
 * notice above the composer, the hint below it, and the picker.
 */
function controls() {
  const task = state.task;
  const working =
    Boolean(task && ["queued", "running"].includes(task.status)) ||
    (Boolean(state.active) && state.active === state.id);
  const waiting = Boolean(task?.queued_message);
  // While another chat works, a message sent here waits in the queue instead.
  const queueing = Boolean(state.active) && state.active !== state.id;
  // What stops any message to this chat, whether typed or picked from choices.
  const blocked =
    !state.catalog ||
    state.settingsBusy ||
    state.busy ||
    state.loading ||
    working ||
    waiting ||
    (state.id !== null && !task?.can_continue);
  $("send").disabled =
    blocked || preparingCount(state.files) > 0 || !$("message").value.trim();
  for (const button of document.querySelectorAll("#messages .choice"))
    button.disabled = blocked;
  $("send").setAttribute(
    "aria-label",
    queueing ? "Add message to queue" : "Send message",
  );
  $("send").title = queueing
    ? "Add to the queue. It starts when the running chat finishes."
    : "";
  $("send").classList.toggle("queueing", queueing);
  $("cancel").hidden = !task || !["queued", "running"].includes(task.status);
  $("cancel").disabled = state.busy;
  $("new-chat").disabled = state.busy || state.settingsBusy;
  $("message").readOnly = state.busy;
  $("attach").disabled =
    state.busy ||
    state.files.length + preparingCount(state.files) >= UPLOADS_PER_MESSAGE;
  $("compose-hint").textContent = state.id
    ? task?.status === "in_queue"
      ? "This chat starts when its message leaves the queue."
      : "Continue this conversation with its saved context."
    : "A new chat starts a separate conversation.";
  let notice = "";
  if (waiting)
    notice =
      "This chat's message is waiting in the queue and has not been sent yet. You can edit or remove it above.";
  else if (working)
    notice =
      "Codex is working. Your response will appear here when it finishes.";
  else if (task && !task.can_continue)
    notice =
      "This chat has no saved session to continue. Start a new chat and include the context you need.";
  else if (queueing)
    notice = `Another chat is working. You can send this message now; it will wait in the queue and start on its own when the running chat ${
      state.queue.length
        ? `and the ${state.queue.length === 1 ? "message" : `${state.queue.length} messages`} already in the queue finish`
        : "finishes"
    }.`;
  $("notice").textContent = notice;
  $("notice").hidden = !notice;
  renderPicker();
}
/** Build the small pin icon shown beside pinned chat titles. */
function pinIcon() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("pin-icon");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute(
    "d",
    "M8 2h8v2l-1.5 1v5.5l3.5 3V16h-5v6l-1 1-1-1v-6H6v-2.5l3.5-3V5L8 4z",
  );
  svg.append(path);
  return svg;
}
/** Build the small clock icon that marks a message waiting in the queue. */
function queueIcon() {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  svg.classList.add("queue-icon");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zm0 4.5V12l3 2");
  svg.append(path);
  return svg;
}
/** Find a loaded sidebar chat by task id. */
function chatById(id) {
  return state.chats.find((chat) => chat.task_id === id);
}
/** Sort pinned chats first, then most recently updated, then by id for stability. */
function chatOrder(a, b) {
  return (
    Number(Boolean(b.pinned)) - Number(Boolean(a.pinned)) ||
    (b.updated_at || b.created_at).localeCompare(
      a.updated_at || a.created_at,
    ) ||
    b.task_id.localeCompare(a.task_id)
  );
}
/**
 * Build one sidebar row with its title, summary, status, and actions button.
 * A row for a message waiting in the queue shows that message and its place
 * in line instead of the chat's last answer.
 */
function renderRow(chat, queued = null) {
  const title = chat.title || "Untitled chat";
  const row = textNode("div", "", "chat-row");
  row.classList.toggle("current", chat.task_id === state.id);
  row.classList.toggle("menu-open", chat.task_id === menu.id);
  row.classList.toggle("queued", Boolean(queued));
  const main = textNode("button", "", "row-main");
  main.type = "button";
  main.dataset.taskId = chat.task_id;
  main.setAttribute("aria-current", String(chat.task_id === state.id));
  const heading = textNode("span", "", "row-title");
  if (queued) heading.append(queueIcon());
  else if (chat.pinned) heading.append(pinIcon());
  heading.append(textNode("span", title));
  main.append(heading);
  main.append(
    textNode(
      "span",
      queued
        ? markdownPlain(queued.message) || queued.message
        : markdownPlain(chat.summary || chat.question) ||
            labels[chat.status] ||
            chat.status,
      "row-preview",
    ),
  );
  const meta = textNode("span", "", "row-meta");
  meta.append(
    textNode(
      "span",
      queued
        ? queued.position === 1
          ? "Next in queue"
          : `In queue · ${queued.position} of ${state.queue.length}`
        : labels[chat.status] || chat.status,
      queued || chat.status === "waiting_for_input" ? "waiting" : "",
    ),
  );
  meta.append(
    textNode(
      "span",
      dateLabel(queued ? queued.created_at : chat.updated_at || chat.created_at),
    ),
  );
  main.append(meta);
  main.onclick = () => selectChat(chat.task_id);
  row.append(main);
  // A chat that is still waiting for its first message has nothing to pin or rename yet.
  if (!chatById(chat.task_id)) return row;
  const actions = textNode("button", "⋯", "row-menu");
  actions.type = "button";
  actions.dataset.menuFor = chat.task_id;
  actions.setAttribute("aria-label", `Chat actions for ${title}`);
  actions.setAttribute("aria-haspopup", "menu");
  actions.setAttribute("aria-expanded", String(chat.task_id === menu.id));
  actions.onclick = () =>
    chat.task_id === menu.id
      ? closeChatMenu(true)
      : openChatMenu(chat.task_id, actions);
  row.append(actions);
  return row;
}
/**
 * Redraw the sidebar chat list, under Pinned and Recent headings when any chat
 * is pinned. Does nothing when the list is unchanged; otherwise keeps the
 * focus and an open chat actions menu on the rows that replace the old ones.
 */
function renderList() {
  const signature = JSON.stringify([
    state.chats,
    state.id,
    menu.id,
    state.queue,
  ]);
  if (signature === state.listSignature) return;
  state.listSignature = signature;
  const list = $("chat-list");
  const focused = document.activeElement;
  const focusId = focused?.dataset.taskId || focused?.dataset.menuFor;
  const focusMenu = Boolean(focused?.dataset.menuFor);
  list.replaceChildren();
  // Chats with a waiting message are listed once, in the order they will start.
  const waiting = new Set(state.queue.map((entry) => entry.task_id));
  const chats = state.chats.filter((chat) => !waiting.has(chat.task_id));
  const pinned = chats.filter((chat) => chat.pinned);
  if (state.queue.length) list.append(textNode("p", "In queue", "list-group"));
  for (const entry of state.queue)
    list.append(
      renderRow(
        chatById(entry.task_id) || {
          task_id: entry.task_id,
          title: entry.title,
        },
        entry,
      ),
    );
  const groups =
    pinned.length || state.queue.length
      ? [
          ["Pinned", pinned],
          ["Recent", chats.filter((chat) => !chat.pinned)],
        ]
      : [["", chats]];
  for (const [label, rows] of groups) {
    if (label && rows.length) list.append(textNode("p", label, "list-group"));
    for (const chat of rows) list.append(renderRow(chat));
  }
  if (focusId)
    list
      .querySelector(
        focusMenu
          ? `[data-menu-for="${CSS.escape(focusId)}"]`
          : `[data-task-id="${CSS.escape(focusId)}"]`,
      )
      ?.focus();
  if (menu.id) {
    // Keep an open menu attached to the row that replaced its trigger.
    const trigger = list.querySelector(
      `[data-menu-for="${CSS.escape(menu.id)}"]`,
    );
    if (trigger) {
      menu.trigger = trigger;
      if (!$("chat-menu").classList.contains("sheet")) positionChatMenu();
    } else closeChatMenu();
  }
  $("list-empty").hidden = state.chats.length + state.queue.length > 0;
  $("load-more").hidden = state.next === null;
}
/**
 * Fetch the chat list from the worker: a refresh of the chats already loaded,
 * or the next 20 when older is set. Updates state.chats and state.active,
 * redraws the list, and opens a new chat when the open one is gone.
 */
async function loadList(older = false) {
  const count = older ? 20 : Math.max(20, Math.min(500, state.chats.length));
  const offset = older ? state.chats.length : 0;
  const data = await api(
    `tasks?summary=true&order=pinned_first&limit=${count}&offset=${offset}`,
  );
  // A full refresh covers every loaded chat, so drop entries the server no
  // longer returns, such as a chat deleted here or in another tab.
  const kept = older || state.chats.length > 500 ? state.chats : [];
  const hadSelected = state.chats.some((chat) => chat.task_id === state.id);
  const merged = new Map(kept.map((chat) => [chat.task_id, chat]));
  for (const chat of data.tasks) merged.set(chat.task_id, chat);
  state.chats = [...merged.values()].sort(chatOrder);
  state.next = state.chats.length < data.total ? state.chats.length : null;
  state.active = data.active_task_id;
  state.queue = Array.isArray(data.queue) ? data.queue : [];
  if (hadSelected && !merged.has(state.id)) await selectChat(null);
  renderList();
  controls();
}
/** Return the image attachments in a list that carry a well-formed id. */
function imageAttachments(list) {
  return (Array.isArray(list) ? list : []).filter(
    (item) =>
      item &&
      item.kind === "image" &&
      typeof item.attachment_id === "string" &&
      /^[0-9a-f]{32}$/.test(item.attachment_id),
  );
}
/** Build a worker-relative attachment URL from ids only, never from server text. */
function attachmentUrl(taskId, attachment, download = false) {
  return (
    `tasks/${encodeURIComponent(taskId)}/attachments/` +
    encodeURIComponent(attachment.attachment_id) +
    (download ? "?download=1" : "")
  );
}
/** Format a byte count as KB or MB for the attachment caption. */
function sizeLabel(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  return bytes < 1024 * 1024
    ? `${Math.max(1, Math.round(bytes / 1024))} KB`
    : `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
/** Render image attachments with a full-size link and a download button. */
function renderAttachments(
  taskId,
  attachments,
  alt = "Image generated by Codex",
  className = "attachments",
) {
  const list = textNode("div", "", className);
  for (const attachment of attachments) {
    const figure = textNode("figure", "", "attachment");
    const link = document.createElement("a");
    link.href = attachmentUrl(taskId, attachment);
    link.target = "_blank";
    link.rel = "noreferrer noopener";
    link.title = "Open full size";
    const img = document.createElement("img");
    img.className = "attachment-image";
    img.src = link.href;
    img.alt = attachment.revised_prompt || alt;
    img.loading = "lazy";
    img.decoding = "async";
    img.onerror = () =>
      figure.replaceChildren(
        textNode(
          "p",
          "This image is no longer available.",
          "attachment-missing",
        ),
      );
    link.append(img);
    const caption = textNode("figcaption", "");
    caption.append(
      textNode(
        "span",
        [attachment.name, sizeLabel(attachment.size)]
          .filter(Boolean)
          .join(" · "),
      ),
    );
    const download = textNode("a", "Download");
    download.href = attachmentUrl(taskId, attachment, true);
    download.setAttribute("download", attachment.name || "image");
    caption.append(download);
    figure.append(link, caption);
    list.append(figure);
  }
  return list;
}
// Home Assistant's own configuration check runs after YAML changes; say how it went.
const checkLabels = {
  valid: "Home Assistant configuration check passed",
  invalid: "Home Assistant configuration check failed",
  unavailable: "Home Assistant could not check the configuration",
};
/**
 * Build the line that says how Home Assistant's configuration check went, with
 * any errors and warnings. Returns null when there is no known result.
 */
function renderConfigCheck(check) {
  if (!check || !checkLabels[check.result]) return null;
  const node = textNode("div", "", `check check-${check.result}`);
  node.append(
    textNode(
      "span",
      { valid: "✓", invalid: "✕", unavailable: "?" }[check.result],
      "check-mark",
    ),
    textNode("span", checkLabels[check.result]),
  );
  if (check.result !== "valid" && check.errors)
    node.append(textNode("pre", check.errors, "check-detail"));
  if (check.warnings)
    node.append(textNode("pre", `Warnings: ${check.warnings}`, "check-detail"));
  return node;
}
/**
 * Say which changed files have a copy of their previous version and which have
 * none. Copies are deleted after the retention period set in the app options.
 */
function renderBackups(turn) {
  const entries = Array.isArray(turn.backups) ? turn.backups : [];
  const saved = entries.filter((entry) => entry.status === "saved");
  const unsaved = entries.filter((entry) =>
    ["missing", "unverified"].includes(entry.status),
  );
  /** Word a number of files: "1 file" or "3 files". */
  const count = (list) =>
    list.length === 1 ? "1 file" : `${list.length} files`;
  const nodes = [];
  if (saved.length) {
    const node = textNode("div", "", "check check-valid backups-saved");
    const days = state.catalog?.backup_retention_days;
    node.append(
      textNode("span", "✓", "check-mark"),
      textNode(
        "span",
        turn.backups_removed
          ? `Previous version saved for ${count(saved)}; the copies have since been removed`
          : `Previous version saved for ${count(saved)}${days ? `, kept for ${days === 1 ? "1 day" : `${days} days`}` : ""}`,
      ),
    );
    if (!turn.backups_removed)
      node.append(
        textNode(
          "pre",
          saved.map((entry) => `${entry.path} → ${entry.copy}`).join("\n"),
          "check-detail",
        ),
      );
    nodes.push(node);
  }
  if (unsaved.length) {
    const node = textNode("div", "", "check backups-missing");
    node.append(
      textNode("span", "!", "check-mark"),
      textNode("span", `No previous version saved for ${count(unsaved)}`),
      textNode(
        "pre",
        unsaved.map((entry) => entry.path).join("\n"),
        "check-detail",
      ),
    );
    nodes.push(node);
  }
  return nodes;
}
/** "12s", "1m 05s", or "1h 02m": how long the running exchange has taken so far. */
function elapsedLabel(ms) {
  const total = Math.max(0, Math.floor(ms / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = String(total % 60);
  if (hours) return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  return minutes ? `${minutes}m ${seconds.padStart(2, "0")}s` : `${seconds}s`;
}
/** Show the running time next to the waiting line and the step list heading. */
function tickElapsed() {
  const activity = state.activity;
  const running =
    activity &&
    activity.id === state.id &&
    activity.startedAt !== null &&
    (activity.running || taskActive());
  for (const node of document.querySelectorAll(".elapsed"))
    node.textContent = running
      ? ` · ${elapsedLabel(Date.now() - activity.startedAt)}`
      : "";
}
/**
 * Build the Verification section of an answer: one expandable row per check
 * with its result, details, and findings, then the screenshots that have not
 * expired. Returns null when the turn has no checks.
 */
function renderVerification(turn, taskId) {
  const checks = turn.verification || [];
  if (!checks.length) return null;
  const section = textNode("section", "", "verification");
  section.append(textNode("h3", "Verification"));
  for (const check of checks) {
    const row = textNode("details", "", `verification-result is-${check.status}`);
    const label = {
      passed: "Passed", failed: "Failed", observed: "Readback", captured: "Screenshots captured",
      issues: "Needs review", disabled: "Disabled", unavailable: "Unverified",
    }[check.status] || "Unverified";
    row.append(textNode("summary", `${label} · ${check.entity_id || check.path || check.target || check.operation}`));
    if (check.message) row.append(textNode("pre", check.message));
    if (check.state !== undefined) row.append(textNode("p", `State: ${check.state}${check.expected_state !== null ? ` · Expected: ${check.expected_state}` : ""}`));
    if (check.attributes && Object.keys(check.attributes).length) row.append(textNode("pre", JSON.stringify(check.attributes, null, 2)));
    if (Array.isArray(check.findings) && check.findings.length) {
      const groups = {
        resource: "Resources that could not load", home_assistant: "Home Assistant errors",
        dashboard: "Dashboard errors", policy: "Blocked actions and requests",
        diagnostic: "Blocked diagnostic logging and notifications",
      };
      for (const [kind, title] of Object.entries(groups)) {
        const findings = check.findings.filter(finding => finding.kind === kind);
        if (!findings.length) continue;
        row.append(textNode("h4", title));
        for (const finding of findings) {
          row.append(textNode("p", finding.message));
          const views = Array.isArray(finding.viewports) ? finding.viewports.join(", ") : "";
          row.append(textNode("small", `${finding.count > 1 ? `${finding.count} occurrences` : "1 occurrence"}${views ? ` · ${views}` : ""}`));
        }
      }
      if (check.findings_omitted) row.append(textNode("p", "Additional findings were omitted because the evidence limit was reached."));
    } else {
      for (const error of [...(Array.isArray(check.errors) ? check.errors : []), ...(Array.isArray(check.blocked) ? check.blocked : [])]) row.append(textNode("p", error));
    }
    if (check.checked_at) row.append(textNode("small", dateLabel(check.checked_at, true)));
    section.append(row);
  }
  const images = imageAttachments(turn.verification_attachments).filter(image => image.expires_at * 1000 > Date.now());
  if (images.length) section.append(renderAttachments(taskId, images, "Dashboard verification screenshot"));
  section.append(textNode("p", "Screenshots are evidence for visual review. State and configuration checks do not prove automation behavior.", "verification-note"));
  return section;
}
/** Draw the open chat: every exchange with its answer, checks, saved copies, and steps. */
function renderTask(force = false) {
  const task = state.task;
  if (!task) return;
  const queued = task.queued_message || null;
  if (state.edit && state.edit.id !== queued?.queue_id) state.edit = null;
  $("chat-title").textContent = task.title || "Untitled chat";
  $("chat-status").textContent =
    task.status === "in_queue"
      ? "In queue · not sent yet"
      : `${labels[task.status] || task.status} · ${dateLabel(task.updated_at, true)}${queued ? " · next message in queue" : ""}`;
  const signature = JSON.stringify([
    task.turns,
    task.history_incomplete,
    task.status,
    queued && [queued.queue_id, queued.message, queued.prompt_attachments],
    state.edit?.id,
    state.catalog?.backup_retention_days,
  ]);
  if (!force && signature === state.signature) {
    // Its place in line can change without the conversation being redrawn.
    if (queued && $("queued-note"))
      $("queued-note").textContent = queueNote(queued);
    controls();
    ensureActivity();
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
  const turns = task.turns || [];
  for (const turn of turns) {
    const latest = turn === turns[turns.length - 1];
    const exchange = textNode("section", "", "exchange");
    const sent = imageAttachments(turn.prompt_attachments);
    if (sent.length)
      exchange.append(
        renderAttachments(
          task.task_id,
          sent,
          "Image attached to your message",
          "attachments user-attachments",
        ),
      );
    if (turn.message)
      exchange.append(renderMarkdown(turn.message, "message user"));
    const attachments = imageAttachments(turn.attachments);
    const hasAnswer =
      turn.summary || turn.details || turn.question || attachments.length || turn.verification?.length;
    if (hasAnswer) {
      const answer = textNode("div", "", "answer");
      const heading = textNode("div", "", "answer-heading");
      heading.append(textNode("span", "⌘", "mark"), textNode("span", "Codex"));
      answer.append(heading);
      if (turn.summary) answer.append(renderMarkdown(turn.summary, "message"));
      if (turn.details && turn.details !== turn.summary)
        answer.append(renderMarkdown(turn.details, "message details"));
      if (turn.question && turn.question !== turn.summary)
        answer.append(renderMarkdown(turn.question, "message question"));
      // Only the question still waiting can be answered with a choice.
      if (
        latest &&
        !queued &&
        task.status === "waiting_for_input" &&
        turn.choices?.length
      )
        answer.append(renderChoices(turn));
      const check = renderConfigCheck(turn.config_check);
      if (check) answer.append(check);
      answer.append(...renderBackups(turn));
      const evidence = renderVerification(turn, task.task_id);
      if (evidence) answer.append(evidence);
      if (attachments.length)
        answer.append(renderAttachments(task.task_id, attachments));
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
      if (latest) answer.append(activityBlock());
      exchange.append(answer);
    } else if (["queued", "running"].includes(turn.status)) {
      const pending = textNode(
        "p",
        turn.status === "queued"
          ? "Getting started…"
          : "Working on your request…",
        "pending",
      );
      if (latest) {
        pending.id = "pending";
        pending.append(textNode("span", "", "elapsed"));
      }
      exchange.append(pending);
      if (latest) exchange.append(activityBlock());
    }
    area.append(exchange);
  }
  if (queued) area.append(renderQueued(task.task_id, queued));
  renderActivity();
  area.scrollTop = force || nearBottom ? area.scrollHeight : oldScroll;
  controls();
  ensureActivity();
}
/**
 * Build a button for each answer Codex offered with its question. A click
 * sends that answer as the next message; the message box stays free for a
 * different one.
 */
function renderChoices(turn) {
  const group = textNode("div", "", "choices");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", "Answers Codex offers");
  turn.choices.forEach((choice, index) => {
    const button = textNode(
      "button",
      markdownPlain(choice) || choice,
      "subtle choice",
    );
    button.type = "button";
    button.onclick = () => sendChoice(turn.turn_id, index);
    group.append(button);
  });
  return group;
}
/**
 * Answer the waiting question with one of the choices Codex offered. The turn
 * id lets the worker refuse the answer when the question was already answered
 * somewhere else. A draft in the message box stays as it is.
 */
async function sendChoice(turnId, index) {
  const id = state.id;
  if (!id || state.busy) return;
  state.busy = true;
  controls();
  showError(null);
  try {
    const result = await api(`tasks/${encodeURIComponent(id)}/continue`, {
      choice: index,
      turn_id: turnId,
      chat_settings: selectedSettings(),
      // If another chat is working, the worker holds this answer in its queue.
      queue: true,
    });
    state.chatSettings.delete(id);
    if (result.status !== "in_queue") state.active = result.task_id;
    state.busy = false;
    await selectChat(result.task_id);
    await loadList();
    if (result.status === "in_queue") $("chat-list").scrollTop = 0;
  } catch (error) {
    showError(error);
    // The question may have been answered elsewhere; show where the chat stands.
    await Promise.allSettled([loadList(), fetchSelected(true)]);
  } finally {
    state.busy = false;
    controls();
  }
}
/**
 * Show a message that waits in the queue: outlined rather than filled, so it
 * reads as not sent yet, with controls to edit or remove it.
 */
function renderQueued(taskId, entry) {
  const exchange = textNode("section", "", "exchange queued");
  const label = textNode("p", "", "queued-label");
  label.append(queueIcon(), textNode("span", "In queue · not sent yet"));
  exchange.append(label);
  const sent = imageAttachments(entry.prompt_attachments);
  if (sent.length) {
    const images = renderAttachments(
      taskId,
      sent,
      "Image attached to your queued message",
      "attachments user-attachments",
    );
    // A loading image pushes the edit and remove buttons down; keep them in view.
    for (const img of images.querySelectorAll("img"))
      img.addEventListener("load", () => {
        const area = $("messages");
        if (
          area.scrollHeight - area.scrollTop - area.clientHeight <
          img.height + 100
        )
          area.scrollTop = area.scrollHeight;
      });
    exchange.append(images);
  }
  const edit = state.edit?.id === entry.queue_id ? state.edit : null;
  const actions = textNode("div", "", "queued-actions");
  const button = (text, className, onclick) => {
    const node = textNode("button", text, className);
    node.type = "button";
    node.disabled = Boolean(edit?.saving);
    node.onclick = onclick;
    actions.append(node);
    return node;
  };
  if (edit) {
    const input = document.createElement("textarea");
    input.id = "queued-edit";
    input.className = "queued-edit";
    input.rows = 2;
    input.value = edit.text;
    input.readOnly = Boolean(edit.saving);
    input.setAttribute("aria-label", "Edit queued message");
    const resize = () => {
      input.style.height = "auto";
      input.style.height = Math.min(320, input.scrollHeight + 2) + "px";
    };
    input.addEventListener("input", () => {
      edit.text = input.value;
      save.disabled = !input.value.trim();
      resize();
    });
    input.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        cancelEdit();
      } else if (
        event.key === "Enter" &&
        !event.shiftKey &&
        !event.isComposing &&
        !matchMedia("(max-width:700px)").matches
      ) {
        event.preventDefault();
        saveEdit();
      }
    });
    exchange.append(input);
    button("Cancel", "subtle", cancelEdit);
    const save = button("Save", "", saveEdit);
    save.id = "queued-save";
    save.disabled = Boolean(edit.saving) || !edit.text.trim();
    // Size and focus the box once it is in the page.
    requestAnimationFrame(() => {
      if (!input.isConnected) return;
      resize();
      input.focus();
      input.setSelectionRange(input.value.length, input.value.length);
    });
  } else {
    exchange.append(renderMarkdown(entry.message, "message user queued"));
    const note = textNode("span", queueNote(entry), "queued-note");
    note.id = "queued-note";
    actions.append(note);
    button("Edit", "subtle", () => startEdit(entry)).id = "queued-edit-button";
    button("Remove", "subtle danger", () => removeQueued(entry)).id =
      "queued-remove";
  }
  exchange.append(actions);
  return exchange;
}
/** Open the editor for a waiting message, starting from its current text. */
function startEdit(entry) {
  state.edit = { id: entry.queue_id, text: entry.message, saving: false };
  showError(null);
  renderTask(true);
}
/** Close the editor and keep the waiting message as it was. */
function cancelEdit() {
  state.edit = null;
  renderTask(true);
}
/** Refresh the open chat and the list after the queue changed. */
async function reloadQueue() {
  await Promise.allSettled([fetchSelected(true), loadList()]);
}
/** Save the edited text of a waiting message. */
async function saveEdit() {
  const edit = state.edit;
  const message = edit?.text.trim();
  if (!message || edit.saving) return;
  edit.saving = true;
  renderTask(true);
  showError(null);
  try {
    await api(`queue/${encodeURIComponent(edit.id)}`, { message });
    if (state.edit === edit) state.edit = null;
  } catch (error) {
    showError(error);
    edit.saving = false;
    // A 404 means it started while it was being edited, so there is nothing left to edit.
    if (error.status === 404 && state.edit === edit) state.edit = null;
    // Unlock the editor even if the refresh below cannot reach the worker.
    renderTask(true);
  }
  await reloadQueue();
}
let removing = false;
/** Take a waiting message out of the queue; a chat that never started goes with it. */
async function removeQueued(entry) {
  if (removing) return;
  removing = true;
  const id = state.id;
  showError(null);
  try {
    await api(
      `queue/${encodeURIComponent(entry.queue_id)}`,
      undefined,
      "DELETE",
    );
    if (state.id === id && state.task?.status === "in_queue")
      await selectChat(null);
  } catch (error) {
    showError(error);
  }
  await reloadQueue();
  removing = false;
}
// Steps Codex reports while it works on the latest message: reasoning
// headlines, progress notes, commands, file edits, searches, and tool calls.
const stepIcons = {
  reasoning: "✦",
  message: "❝",
  command: "›",
  file_change: "✎",
  web_search: "⌕",
  tool_call: "⚙",
  plan: "☰",
  error: "!",
  outcome: "■",
  notice: "…",
  // What the worker itself is doing, before and after Codex runs.
  phase: "◦",
  other: "•",
};
/** Return whether the open chat's task is queued or running. */
function taskActive() {
  return Boolean(
    state.task && ["queued", "running"].includes(state.task.status),
  );
}
/** Forget the steps of the previous chat and stop polling for them. */
function resetActivity(id) {
  clearTimeout(activityTimer);
  activityTimer = null;
  state.activity = id
    ? {
        id,
        turnId: null,
        seq: 0,
        steps: new Map(),
        running: false,
        startedAt: null,
        loaded: false,
        loading: false,
        expanded: null,
        open: new Set(),
      }
    : null;
}
/** Build the empty, hidden container that renderActivity fills with steps. */
function activityBlock() {
  const block = textNode("div", "", "activity");
  block.id = "activity";
  block.hidden = true;
  return block;
}
/** Load steps once for an idle chat, and keep loading them while Codex works. */
function ensureActivity() {
  const activity = state.activity;
  if (!activity || activity.id !== state.id || activityTimer || activity.loading)
    return;
  // A chat that is still waiting in the queue has no steps to load yet.
  if (state.task?.status === "in_queue") return;
  if (!activity.loaded || activity.running || taskActive()) pollActivity();
}
/** Fetch the steps added since the last poll and merge them by position. */
async function pollActivity() {
  clearTimeout(activityTimer);
  activityTimer = null;
  const activity = state.activity;
  const id = state.id;
  if (!activity || !id || activity.id !== id || activity.loading) return;
  if (document.hidden) {
    activityTimer = setTimeout(pollActivity, 1000);
    return;
  }
  activity.loading = true;
  const generation = state.generation;
  let restart = false;
  try {
    const data = await api(
      `tasks/${encodeURIComponent(id)}/activity?after=${activity.seq}`,
    );
    if (generation !== state.generation || state.activity !== activity) return;
    if (activity.turnId !== null && data.turn_id !== activity.turnId) {
      // A new exchange started, so its steps count from the beginning again.
      activity.steps.clear();
      activity.open.clear();
      activity.expanded = null;
      activity.seq = 0;
      activity.turnId = data.turn_id;
      activity.startedAt = null;
      restart = true;
      return;
    }
    const wasRunning = activity.running;
    activity.turnId = data.turn_id;
    for (const step of data.steps) activity.steps.set(step.index, step);
    activity.seq = data.seq;
    activity.running = data.running;
    activity.loaded = true;
    if (data.running && typeof data.elapsed_ms === "number") {
      const startedAt = Date.now() - data.elapsed_ms;
      // Keep the first reading unless it is clearly off, so the seconds tick evenly.
      if (
        activity.startedAt === null ||
        Math.abs(startedAt - activity.startedAt) > 1500
      )
        activity.startedAt = startedAt;
    } else activity.startedAt = null;
    if (data.steps.length || wasRunning !== data.running) renderActivity();
    tickElapsed();
    if (!data.running && taskActive()) fetchSelected().catch(showError);
  } catch (error) {
    // The regular refresh reports worker problems; the steps just pause.
  } finally {
    activity.loading = false;
    if (state.activity === activity) {
      if (restart) pollActivity();
      else if (activity.running || taskActive())
        activityTimer = setTimeout(pollActivity, 1000);
    }
  }
}
/** Draw the step list of the latest exchange, with its heading, count, and timer. */
function renderActivity() {
  const block = $("activity");
  const activity = state.activity;
  if (!block || !activity || activity.id !== state.id) return;
  const steps = [...activity.steps.values()].sort((a, b) => a.index - b.index);
  const running = activity.running || taskActive();
  block.hidden = !steps.length;
  block.classList.toggle("running", running);
  // The step list takes the place of the waiting line once Codex reports steps.
  const pending = $("pending");
  if (pending) pending.hidden = !block.hidden;
  if (block.hidden) {
    block.replaceChildren();
    return;
  }
  const expanded = activity.expanded ?? running;
  const area = $("messages");
  const nearBottom =
    area.scrollHeight - area.scrollTop - area.clientHeight < 100;
  const count = steps.length === 1 ? "1 step" : `${steps.length} steps`;
  const toggle = textNode("button", "", "activity-toggle");
  toggle.type = "button";
  toggle.setAttribute("aria-expanded", String(expanded));
  toggle.setAttribute("aria-controls", "activity-steps");
  const label = textNode(
    "span",
    running
      ? `Working on your request… · ${count}`
      : `${expanded ? "Hide" : "Show"} activity (${count})`,
  );
  if (running) label.append(textNode("span", "", "elapsed"));
  toggle.append(textNode("span", "", "activity-chevron"), label);
  toggle.onclick = () => {
    activity.expanded = !expanded;
    renderActivity();
  };
  const list = textNode("div", "", "activity-steps");
  list.id = "activity-steps";
  list.hidden = !expanded;
  for (const step of steps) list.append(renderStep(step, activity));
  block.replaceChildren(toggle, list);
  tickElapsed();
  if (nearBottom) area.scrollTop = area.scrollHeight;
}
/**
 * Build one row of the step list: icon, text, and a line with a failed
 * command's exit code, the duration, and the running state. A step with
 * output gets a button that shows or hides it.
 */
function renderStep(step, activity) {
  const row = textNode("div", "", `step step-${step.kind} is-${step.status}`);
  row.append(textNode("span", stepIcons[step.kind] || "•", "step-icon"));
  const body = textNode("div", "", "step-body");
  body.append(textNode("div", step.text, "step-text"));
  const meta = [];
  if (step.kind === "command" && step.exit_code)
    meta.push(`exit ${step.exit_code}`);
  if (step.duration_ms >= 1000)
    meta.push(
      `${(step.duration_ms / 1000).toFixed(step.duration_ms >= 10000 ? 0 : 1)} s`,
    );
  if (step.status === "running") meta.push("running");
  const footer = textNode("div", "", "step-meta");
  if (meta.length) footer.append(textNode("span", meta.join(" · ")));
  if (step.output) {
    const open = activity.open.has(step.index);
    const button = textNode(
      "button",
      open
        ? "Hide output"
        : step.output_truncated
          ? "Show output (first part)"
          : "Show output",
      "step-output-toggle",
    );
    button.type = "button";
    button.onclick = () => {
      if (open) activity.open.delete(step.index);
      else activity.open.add(step.index);
      renderActivity();
    };
    footer.append(button);
    if (open) body.append(textNode("pre", step.output, "step-output"));
  }
  if (footer.childElementCount) body.insertBefore(footer, body.children[1] || null);
  row.append(body);
  return row;
}
/**
 * Fetch the open chat from the worker into state.task and draw it; force
 * redraws it even when nothing changed. A response that arrives after another
 * chat was opened is dropped.
 */
async function fetchSelected(force = false) {
  const id = state.id;
  const generation = state.generation;
  if (!id) return;
  let data;
  try {
    data = await api(`tasks/${encodeURIComponent(id)}`);
  } catch (error) {
    // A chat that only existed as a queued message is gone once that message
    // is removed, here or in another tab.
    if (
      error.status === 404 &&
      state.task?.status === "in_queue" &&
      generation === state.generation &&
      id === state.id
    )
      return selectChat(null);
    throw error;
  }
  if (generation !== state.generation || id !== state.id) return;
  state.task = data.task;
  if (!state.chatSettings.has(id))
    state.chatSettings.set(
      id,
      data.task.chat_settings || { model: null, reasoning_effort: null },
    );
  state.loading = false;
  renderTask(force);
}
const LAST_CHAT_KEY = "codex-last-chat";
/** Remember the open chat, or forget it for a new chat, so a reload returns to it. */
function rememberChat(id) {
  try {
    if (id) localStorage.setItem(LAST_CHAT_KEY, id);
    else localStorage.removeItem(LAST_CHAT_KEY);
  } catch (_) {
    /* Storage may be disabled. */
  }
}
/** Return the remembered chat id when it looks like a task id, else null. */
function rememberedChat() {
  try {
    const id = localStorage.getItem(LAST_CHAT_KEY);
    return id && /^[A-Za-z0-9_-]{1,80}$/.test(id) ? id : null;
  } catch (_) {
    return null;
  }
}
/**
 * Open the remembered chat at once, before the list loads, so the welcome
 * screen is not shown first. Resolves when the chat is loaded or given up.
 */
function reopenLastChat() {
  const id = rememberedChat();
  return id ? selectChat(id, true) : Promise.resolve();
}
/**
 * Open a chat, or the welcome screen for null, keeping the draft of the one
 * left. When restoring a remembered chat, a 404 means it was deleted from
 * another tab or device: forget it and start fresh without an error.
 */
async function selectChat(id, restoring = false) {
  if (state.busy || state.settingsBusy) return;
  closePicker();
  state.drafts.set(state.id, $("message").value);
  state.draftFiles.set(state.id, state.files);
  state.id = id;
  rememberChat(id);
  state.files = state.draftFiles.get(id) || [];
  renderPending();
  state.task = null;
  state.edit = null;
  state.generation++;
  state.signature = "";
  resetActivity(id);
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
    if (state.id !== id) return;
    if (restoring && error.status === 404) {
      rememberChat(null);
      await selectChat(null);
    } else showError(error);
  }
}
const menu = {
  id: null,
  trigger: null,
  point: null,
  returnId: null,
  longPress: null,
  pressStart: null,
  suppressClick: false,
};
/** Show or clear the error line under the chat list. */
function sidebarError(error) {
  $("sidebar-error").textContent = error ? String(error.message || error) : "";
  $("sidebar-error").hidden = !error;
}
/** Show or clear the error line inside the named dialog. */
function dialogError(name, error) {
  $(name + "-error").textContent = error ? String(error.message || error) : "";
  $(name + "-error").hidden = !error;
}
/** Return the enabled, visible items of the chat actions menu. */
function menuItems() {
  return [...$("chat-menu").querySelectorAll('[role="menuitem"]')].filter(
    (item) => !item.disabled && item.offsetParent !== null,
  );
}
/** Place the desktop menu at the pointer or under its trigger, inside the viewport. */
function positionChatMenu() {
  const panel = $("chat-menu");
  panel.style.left = panel.style.top = "0px";
  const width = panel.offsetWidth;
  const height = panel.offsetHeight;
  const rect = menu.trigger?.getBoundingClientRect();
  let left = menu.point ? menu.point.x : rect ? rect.right - width : 8;
  let top = menu.point ? menu.point.y : rect ? rect.bottom + 4 : 8;
  if (!menu.point && rect && top + height > innerHeight - 8)
    top = rect.top - height - 4;
  left = Math.max(8, Math.min(left, innerWidth - width - 8));
  top = Math.max(8, Math.min(top, innerHeight - height - 8));
  panel.style.left = `${left}px`;
  panel.style.top = `${top}px`;
}
/** Hide the chat actions menu and optionally return focus to its trigger. */
function closeChatMenu(focus = false) {
  if ($("chat-menu").hidden) return;
  const trigger = menu.trigger;
  $("chat-menu").hidden = true;
  $("menu-scrim").hidden = true;
  menu.id = null;
  menu.trigger = null;
  menu.point = null;
  for (const button of $("chat-list").querySelectorAll(
    '.row-menu[aria-expanded="true"]',
  ))
    button.setAttribute("aria-expanded", "false");
  for (const row of $("chat-list").querySelectorAll(".chat-row.menu-open"))
    row.classList.remove("menu-open");
  if (focus && trigger?.isConnected) trigger.focus();
}
/** Show the actions for one chat, anchored to its button or as a sheet on phones. */
function openChatMenu(id, trigger, point = null) {
  const chat = chatById(id);
  if (!chat) return;
  if (id === menu.id && !$("chat-menu").hidden) return;
  closeChatMenu();
  closePicker();
  menu.id = id;
  menu.trigger =
    trigger ||
    $("chat-list").querySelector(`[data-menu-for="${CSS.escape(id)}"]`);
  menu.point = point;
  const panel = $("chat-menu");
  $("chat-menu-title").textContent = chat.title || "Untitled chat";
  panel.querySelector('[data-action="pin"]').textContent = chat.pinned
    ? "Unpin chat"
    : "Pin chat";
  const remove = panel.querySelector('[data-action="delete"]');
  const working =
    ["queued", "running"].includes(chat.status) || state.active === id;
  remove.disabled = working;
  remove.title = working ? "Stop this task before deleting it." : "";
  const sheet = matchMedia("(max-width:700px)").matches;
  panel.classList.toggle("sheet", sheet);
  panel.style.left = panel.style.top = "";
  panel.hidden = false;
  $("menu-scrim").hidden = !sheet;
  if (!sheet) positionChatMenu();
  menu.trigger?.setAttribute("aria-expanded", "true");
  menu.trigger?.closest(".chat-row")?.classList.add("menu-open");
  menuItems()[0]?.focus();
}
/** Stop a pending long-press timer and forget where the press started. */
function cancelLongPress() {
  clearTimeout(menu.longPress);
  menu.longPress = null;
  menu.pressStart = null;
}
// A long press opens the menu, so the click released afterwards must not
// select the row, tap the scrim, or hit whatever ends up under the finger.
document.addEventListener(
  "click",
  (event) => {
    if (!menu.suppressClick) return;
    menu.suppressClick = false;
    event.preventDefault();
    event.stopPropagation();
  },
  true,
);
// Some browsers fire no click after a long press, so every new press starts
// fresh instead of letting a stale flag swallow the next tap on the menu.
document.addEventListener(
  "pointerdown",
  () => {
    menu.suppressClick = false;
  },
  true,
);
$("chat-list").addEventListener("pointerdown", (event) => {
  cancelLongPress();
  const row = event.target.closest(".row-main");
  if (!row || !event.isPrimary || !["touch", "pen"].includes(event.pointerType))
    return;
  menu.pressStart = { x: event.clientX, y: event.clientY };
  menu.longPress = setTimeout(() => {
    menu.longPress = null;
    menu.suppressClick = true;
    if (navigator.vibrate) navigator.vibrate(10);
    openChatMenu(row.dataset.taskId);
  }, 500);
});
$("chat-list").addEventListener("pointermove", (event) => {
  if (
    menu.pressStart &&
    Math.hypot(
      event.clientX - menu.pressStart.x,
      event.clientY - menu.pressStart.y,
    ) > 10
  )
    cancelLongPress();
});
for (const type of ["pointerup", "pointercancel", "pointerleave"])
  $("chat-list").addEventListener(type, cancelLongPress);
$("chat-list").addEventListener("contextmenu", (event) => {
  const row = event.target.closest(".chat-row");
  if (!row) return;
  event.preventDefault();
  const id = row.querySelector(".row-main").dataset.taskId;
  // Android raises contextmenu for the same long press; keep one menu and drop the click.
  const touch = Boolean(menu.pressStart);
  if (touch) menu.suppressClick = true;
  cancelLongPress();
  const point =
    !touch && event.clientX && event.clientY
      ? { x: event.clientX, y: event.clientY }
      : null;
  openChatMenu(id, null, point);
});
$("chat-list").addEventListener("keydown", (event) => {
  const row = event.target.closest(".row-main");
  if (
    row &&
    (event.key === "ContextMenu" || (event.shiftKey && event.key === "F10"))
  ) {
    event.preventDefault();
    openChatMenu(row.dataset.taskId);
  }
});
$("chat-list").addEventListener("scroll", () => {
  // Follow the row while it stays in view; close once it scrolls away.
  if ($("chat-menu").hidden || $("chat-menu").classList.contains("sheet"))
    return;
  const list = $("chat-list").getBoundingClientRect();
  const rect = menu.trigger?.getBoundingClientRect();
  if (!rect || rect.bottom < list.top || rect.top > list.bottom)
    closeChatMenu();
  else positionChatMenu();
});
window.addEventListener("resize", () => closeChatMenu());
$("menu-scrim").onclick = () => closeChatMenu();
document.addEventListener("pointerdown", (event) => {
  if (
    !$("chat-menu").hidden &&
    !$("chat-menu").contains(event.target) &&
    event.target !== menu.trigger
  )
    closeChatMenu();
});
$("chat-menu").addEventListener("keydown", (event) => {
  const items = menuItems();
  const index = items.indexOf(document.activeElement);
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation();
    closeChatMenu(true);
  } else if (event.key === "Tab") {
    closeChatMenu(true);
  } else if (["ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) {
    event.preventDefault();
    const next =
      event.key === "Home"
        ? 0
        : event.key === "End"
          ? items.length - 1
          : (index + (event.key === "ArrowDown" ? 1 : -1) + items.length) %
            items.length;
    items[next]?.focus();
  }
});
$("chat-menu").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-action]");
  if (!button || button.disabled) return;
  const id = menu.id;
  const action = button.dataset.action;
  menu.returnId = id;
  closeChatMenu(action === "close" || action === "pin");
  if (action === "pin") await togglePin(id);
  else if (action === "rename") openRename(id);
  else if (action === "delete") openDelete(id);
});
/** Pin or unpin a chat through the worker API and re-sort the list. */
async function togglePin(id) {
  const chat = chatById(id);
  if (!chat) return;
  sidebarError(null);
  try {
    const data = await api(`tasks/${encodeURIComponent(id)}/pin`, {
      pinned: !chat.pinned,
    });
    chat.pinned = data.pinned;
    state.chats.sort(chatOrder);
    renderList();
  } catch (error) {
    sidebarError(error);
  }
}
/** Open the rename dialog prefilled with the chat's current title. */
function openRename(id) {
  const chat = chatById(id);
  if (!chat) return;
  $("rename-dialog").dataset.chatId = id;
  $("rename-input").value = chat.title || "";
  $("rename-save").disabled = false;
  dialogError("rename", null);
  $("rename-dialog").showModal();
  $("rename-input").select();
}
$("rename-cancel").onclick = () => $("rename-dialog").close();
$("rename-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const id = $("rename-dialog").dataset.chatId;
  const title = $("rename-input").value.replace(/\s+/g, " ").trim();
  if (!title) {
    dialogError("rename", "Enter a title for this chat.");
    return;
  }
  $("rename-save").disabled = true;
  try {
    const data = await api(`tasks/${encodeURIComponent(id)}/title`, { title });
    const chat = chatById(id);
    if (chat) chat.title = data.title;
    if (state.task?.task_id === id) state.task.title = data.title;
    if (state.id === id) $("chat-title").textContent = data.title;
    renderList();
    $("rename-dialog").close();
  } catch (error) {
    dialogError("rename", error);
  } finally {
    $("rename-save").disabled = false;
  }
});
/** Open the delete confirmation dialog for a chat. */
function openDelete(id) {
  const chat = chatById(id);
  if (!chat) return;
  $("delete-dialog").dataset.chatId = id;
  $("delete-text").textContent =
    `“${chat.title || "Untitled chat"}” and its saved history will be removed. This cannot be undone.`;
  $("delete-confirm").disabled = false;
  dialogError("delete", null);
  $("delete-dialog").showModal();
  $("delete-cancel").focus();
}
$("delete-cancel").onclick = () => $("delete-dialog").close();
for (const name of ["rename", "delete"])
  $(name + "-dialog").addEventListener("close", () => {
    // Return focus to the row's menu button, or to New chat when the row is gone.
    const trigger = menu.returnId
      ? $("chat-list").querySelector(
          `[data-menu-for="${CSS.escape(menu.returnId)}"]`,
        )
      : null;
    menu.returnId = null;
    (trigger || $("new-chat")).focus();
  });
$("delete-confirm").onclick = async () => {
  const id = $("delete-dialog").dataset.chatId;
  $("delete-confirm").disabled = true;
  try {
    await api(`tasks/${encodeURIComponent(id)}`, undefined, "DELETE");
    state.chats = state.chats.filter((chat) => chat.task_id !== id);
    state.chatSettings.delete(id);
    $("delete-dialog").close();
    if (state.id === id) await selectChat(null);
    state.drafts.delete(id);
    state.draftFiles.delete(id);
    renderList();
    await loadList();
  } catch (error) {
    dialogError("delete", error);
  } finally {
    $("delete-confirm").disabled = false;
  }
};
$("composer").addEventListener("submit", async (event) => {
  event.preventDefault();
  if ($("send").disabled) return;
  const message = $("message").value.trim();
  const id = state.id;
  state.busy = true;
  controls();
  showError(null);
  try {
    const attachments = await Promise.all(
      state.files.map(async (upload) => ({
        name: upload.name,
        data: await encodeUpload(upload),
      })),
    );
    const result = await api(
      id ? `tasks/${encodeURIComponent(id)}/continue` : "tasks",
      {
        ...(id ? { message } : { prompt: message }),
        ...(attachments.length ? { attachments } : {}),
        chat_settings: selectedSettings(),
        // If another chat is working, the worker holds this message in its queue.
        queue: true,
      },
    );
    state.drafts.delete(id);
    state.draftFiles.delete(id);
    state.chatSettings.delete(id);
    clearUploads();
    $("message").value = "";
    if (result.status !== "in_queue") state.active = result.task_id;
    state.busy = false;
    await selectChat(result.task_id);
    await loadList();
    // The queue is listed first, so bring the new entry into view.
    if (result.status === "in_queue") $("chat-list").scrollTop = 0;
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
/** Read a pending image as base64 for the JSON request body. */
function encodeUpload(upload) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () =>
      resolve(String(reader.result).split(",", 2)[1] || "");
    reader.onerror = () =>
      reject(new Error(`Could not read ${upload.name}.`));
    reader.readAsDataURL(upload.blob);
  });
}
/** Shrink an image whose longest edge exceeds the limit; other images pass through. */
async function prepareUpload(file) {
  const type = file.type === "image/jpg" ? "image/jpeg" : file.type;
  // Drag-and-drop and clipboard sources can omit the type; the worker sniffs the bytes.
  if (type && !UPLOAD_TYPES.includes(type))
    throw new Error(
      `${file.name || "This file"} is not a PNG, JPEG, GIF, or WebP image.`,
    );
  if (!file.size) {
    // Some mobile pickers hand over a file whose size is unknown until read.
    const bytes = await file.arrayBuffer().catch(() => null);
    if (!bytes || !bytes.byteLength)
      throw new Error(
        `${file.name || "The selected file"} is empty or could not be read from the picker.`,
      );
    file = new File([bytes], file.name || "image", { type });
  }
  let blob = file;
  let name = file.name || "image";
  let bitmap = null;
  try {
    bitmap = await createImageBitmap(file);
  } catch {
    bitmap = null; // Undecodable here; the worker still verifies the bytes.
  }
  if (bitmap) {
    const scale = Math.min(
      1,
      UPLOAD_MAX_EDGE / Math.max(bitmap.width, bitmap.height),
    );
    if (scale < 1 || file.size > UPLOAD_MAX_BYTES) {
      const canvas = document.createElement("canvas");
      canvas.width = Math.max(1, Math.round(bitmap.width * scale));
      canvas.height = Math.max(1, Math.round(bitmap.height * scale));
      canvas
        .getContext("2d")
        .drawImage(bitmap, 0, 0, canvas.width, canvas.height);
      const output = type === "image/jpeg" ? "image/jpeg" : "image/png";
      const resized = await new Promise((resolve) =>
        canvas.toBlob(resolve, output, 0.9),
      );
      if (resized) {
        blob = resized;
        name =
          name.replace(/\.[^.]+$/, "") +
          (output === "image/jpeg" ? ".jpg" : ".png");
      }
    }
    bitmap.close();
  }
  if (blob.size > UPLOAD_MAX_BYTES)
    throw new Error(
      `${file.name || "This image"} is larger than ${UPLOAD_MAX_BYTES / (1024 * 1024)} MB.`,
    );
  return {
    id: globalThis.crypto?.randomUUID
      ? crypto.randomUUID()
      : `${Date.now()}-${Math.random()}`,
    name,
    size: blob.size,
    blob,
    url: URL.createObjectURL(blob),
  };
}
// Images still decoding or resizing, keyed by the pending list they belong to.
const preparing = new Map();
/** Count the images still being prepared for a pending list. */
function preparingCount(list) {
  return preparing.get(list) || 0;
}
/** Add chosen, dropped, or pasted files to the pending list after checks. */
async function addUploads(fileList) {
  const files = Array.from(fileList || []).filter(Boolean);
  if (!files.length) return;
  if (state.busy) {
    showError(new Error("Wait for the current message to finish sending, then attach the image."));
    return;
  }
  // The batch belongs to the draft it was picked in. Switching chats swaps
  // state.files for another list, so every step below works on this one.
  const target = state.files;
  /** Redraw the pending images and controls if this draft is still open. */
  const sync = () => {
    if (target !== state.files) return;
    renderPending();
    controls();
  };
  showError(null);
  for (const file of files) {
    if (target.length + preparingCount(target) >= UPLOADS_PER_MESSAGE) {
      showError(
        new Error(`Attach at most ${UPLOADS_PER_MESSAGE} images per message.`),
      );
      break;
    }
    preparing.set(target, preparingCount(target) + 1);
    sync();
    try {
      target.push(await prepareUpload(file));
    } catch (error) {
      showError(error);
    } finally {
      if (preparingCount(target) > 1)
        preparing.set(target, preparingCount(target) - 1);
      else preparing.delete(target);
      sync();
    }
  }
}
/** Drop one pending image and release its preview. */
function removeUpload(id) {
  const index = state.files.findIndex((item) => item.id === id);
  if (index < 0) return;
  URL.revokeObjectURL(state.files[index].url);
  // Mutate in place: a batch still decoding holds a reference to this list.
  state.files.splice(index, 1);
  renderPending();
  controls();
  $("attach").focus();
}
/** Forget every pending image after a successful send. */
function clearUploads() {
  for (const upload of state.files) URL.revokeObjectURL(upload.url);
  state.files = [];
  renderPending();
}
/** Show the pending images above the message box with remove buttons. */
function renderPending() {
  const strip = $("pending-files");
  strip.replaceChildren();
  for (const upload of state.files) {
    const item = textNode("div", "", "pending-file");
    const img = document.createElement("img");
    img.src = upload.url;
    img.alt = "";
    const name = textNode("span", upload.name, "pending-name");
    name.title = `${upload.name} · ${sizeLabel(upload.size)}`;
    const remove = textNode("button", "×", "pending-remove");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove ${upload.name}`);
    remove.onclick = () => removeUpload(upload.id);
    item.append(img, name, remove);
    strip.append(item);
  }
  for (let count = preparingCount(state.files); count > 0; count--)
    strip.append(textNode("div", "Preparing image…", "pending-file preparing"));
  strip.hidden = !state.files.length && !preparingCount(state.files);
}
// Android WebViews, including the Home Assistant app, ask the system Photo
// Picker for a multi-select when the input allows several files, but their
// file-chooser result handling reads only a single URI, so the page receives
// nothing. Single selection returns a plain URI and works; pick again for more.
if (
  /Android/.test(navigator.userAgent) &&
  /; wv\)|Home Assistant/.test(navigator.userAgent)
)
  $("file-input").removeAttribute("multiple");
$("attach").onclick = () => $("file-input").click();
/** Feed picked, pasted, or dropped files to addUploads and report any surprise. */
async function acceptFiles(fileList) {
  try {
    await addUploads(fileList);
  } catch (error) {
    console.error("Attaching images failed", error);
    showError(
      new Error(
        `Could not attach the image: ${String(error?.message || error)}`,
      ),
    );
  }
}
$("file-input").addEventListener("change", async () => {
  const input = $("file-input");
  await acceptFiles(input.files);
  input.value = "";
});
$("message").addEventListener("paste", (event) => {
  const files = [...(event.clipboardData?.files || [])].filter(
    (file) => !file.type || file.type.startsWith("image/"),
  );
  if (!files.length) return;
  event.preventDefault();
  acceptFiles(files);
});
for (const type of ["dragenter", "dragover"])
  $("composer").addEventListener(type, (event) => {
    if (![...(event.dataTransfer?.types || [])].includes("Files")) return;
    event.preventDefault();
    $("composer").classList.add("dropping");
  });
$("composer").addEventListener("dragleave", (event) => {
  if (!$("composer").contains(event.relatedTarget))
    $("composer").classList.remove("dropping");
});
$("composer").addEventListener("drop", (event) => {
  $("composer").classList.remove("dropping");
  if (!event.dataTransfer?.files?.length) return;
  event.preventDefault();
  acceptFiles(event.dataTransfer.files);
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
/**
 * Set the sidebar width in pixels, kept between 220 and 440 and leaving 360
 * for the chat when the window allows, and remember it for the next visit.
 */
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
/**
 * Show the ChatGPT sign-in status in the settings dialog, with the sign-in
 * code, link, and QR code when the worker provides them. Checks again every
 * three seconds as long as the dialog is open and the sign-in is pending.
 */
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
/** Show an error in the result line of the settings dialog. */
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
let restore = Promise.resolve();
/** Poll the list and the open chat; the first pass waits for the restored chat. */
async function refresh() {
  clearTimeout(refreshTimer);
  if (!document.hidden && !state.busy) {
    try {
      if (!state.catalog) await loadChatOptions();
      await loadList();
      await restore;
      await fetchSelected();
    } catch (error) {
      showError(error);
    }
  }
  refreshTimer = setTimeout(refresh, state.active ? 3000 : 12000);
}
sidebar(false);
controls();
restore = reopenLastChat();
refresh();
loadUsage();
setInterval(tickElapsed, 1000);
