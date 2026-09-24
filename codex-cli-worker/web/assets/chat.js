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
// Mirrors the worker's limits for images attached to a message.
const UPLOAD_MAX_BYTES = 10 * 1024 * 1024;
const UPLOADS_PER_MESSAGE = 6;
const UPLOAD_TYPES = ["image/png", "image/jpeg", "image/gif", "image/webp"];
const UPLOAD_MAX_EDGE = 2048;
const state = {
  id: null,
  task: null,
  chats: [],
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
function selectedSettings() {
  return (
    state.chatSettings.get(state.id) || { model: null, reasoning_effort: null }
  );
}
function selectedModel(settings = selectedSettings()) {
  return state.catalog?.models.find(
    (model) => model.id === (settings.model || state.catalog.defaults.model),
  );
}
function selectedEfforts(settings = selectedSettings()) {
  return (
    selectedModel(settings)?.efforts || state.catalog?.default_efforts || []
  );
}
function effectiveEffort(settings = selectedSettings()) {
  const effort =
    settings.reasoning_effort ||
    state.catalog?.defaults.reasoning_effort ||
    "medium";
  return selectedModel(settings) && !selectedEfforts(settings).includes(effort)
    ? "medium"
    : effort;
}
function closePicker(focus = false) {
  for (const name of ["model", "effort"]) {
    if (focus && !$(name + "-popover").hidden) $(name + "-button").focus();
    $(name + "-popover").hidden = true;
    $(name + "-button").setAttribute("aria-expanded", "false");
  }
}
function renderEffort(effort = effectiveEffort()) {
  const efforts = selectedEfforts();
  const index = Math.max(0, efforts.indexOf(effort));
  $("effort-title").textContent = effortLabels[effort] || effort;
  $("effort-model").textContent = selectedModel()?.label || "Default model";
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
function renderPicker() {
  const settings = selectedSettings();
  const effort = effectiveEffort(settings);
  $("model-button").textContent = settings.model
    ? selectedModel(settings)?.label || settings.model
    : "Default";
  $("model-button").title = settings.model
    ? "Select model"
    : `Use add-on default: ${selectedModel(settings)?.label || "recommended model"}`;
  $("effort-label").textContent = effortLabels[effort] || effort;
  $("effort-button").title = settings.reasoning_effort
    ? "Select reasoning level"
    : "Using add-on default reasoning";
  const disabled =
    !state.catalog ||
    state.loading ||
    state.busy ||
    state.settingsBusy ||
    Boolean(state.task && ["queued", "running"].includes(state.task.status));
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
function openPicker(name) {
  const wasOpen = !$(name + "-popover").hidden;
  closePicker();
  if (wasOpen) return;
  $(name + "-popover").hidden = false;
  $(name + "-button").setAttribute("aria-expanded", "true");
  if (name === "model") {
    const options = $("model-options");
    options.replaceChildren();
    for (const model of [
      { id: null, label: "Default" },
      ...state.catalog.models,
    ]) {
      const button = textNode("button", "", "model-option");
      button.type = "button";
      const selected = model.id === selectedSettings().model;
      button.setAttribute("aria-pressed", String(selected));
      const title = textNode("span", "", "model-option-title");
      title.append(
        textNode("span", model.label),
        textNode("span", selected ? "✓" : "", "model-check"),
      );
      button.append(title);
      if (!model.id) button.append(textNode("small", "Use add-on default"));
      button.onclick = async () => {
        const settings = { ...selectedSettings(), model: model.id };
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
    options.querySelector('[aria-pressed="true"]').focus();
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
async function loadChatOptions() {
  try {
    state.catalog = await api("chat-options");
  } catch (error) {
    showError(error);
  }
  controls();
}
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
    const reset =
      dateLabel(usage[`${key}_reset_at`], true) || usage[`${key}_reset`];
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
/** Send a JSON request to the worker API and return the parsed response. */
async function api(path, body, method) {
  const response = await fetch(path.replace(/^\//, ""), {
    method: method || (body === undefined ? "GET" : "POST"),
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
  if (!open) closeChatMenu();
  document.body.classList.toggle("sidebar-open", open);
  $("scrim").hidden = !open;
  $("menu").setAttribute("aria-expanded", String(open));
  $("sidebar").inert = matchMedia("(max-width:700px)").matches && !open;
  document.querySelector("main").inert =
    matchMedia("(max-width:700px)").matches && open;
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
    !state.catalog ||
    state.settingsBusy ||
    state.busy ||
    state.loading ||
    active ||
    (state.id !== null && !task?.can_continue) ||
    preparingCount(state.files) > 0 ||
    !$("message").value.trim();
  $("cancel").hidden = !task || !["queued", "running"].includes(task.status);
  $("cancel").disabled = state.busy;
  $("new-chat").disabled = state.busy || state.settingsBusy;
  $("message").readOnly = state.busy;
  $("attach").disabled =
    state.busy ||
    state.files.length + preparingCount(state.files) >= UPLOADS_PER_MESSAGE;
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
/** Build one sidebar row with its title, summary, status, and actions button. */
function renderRow(chat) {
  const title = chat.title || "Untitled chat";
  const row = textNode("div", "", "chat-row");
  row.classList.toggle("current", chat.task_id === state.id);
  row.classList.toggle("menu-open", chat.task_id === menu.id);
  const main = textNode("button", "", "row-main");
  main.type = "button";
  main.dataset.taskId = chat.task_id;
  main.setAttribute("aria-current", String(chat.task_id === state.id));
  const heading = textNode("span", "", "row-title");
  if (chat.pinned) heading.append(pinIcon());
  heading.append(textNode("span", title));
  main.append(heading);
  main.append(
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
  meta.append(textNode("span", dateLabel(chat.updated_at || chat.created_at)));
  main.append(meta);
  main.onclick = () => selectChat(chat.task_id);
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
  row.append(main, actions);
  return row;
}
function renderList() {
  const signature = JSON.stringify([state.chats, state.id, menu.id]);
  if (signature === state.listSignature) return;
  state.listSignature = signature;
  const list = $("chat-list");
  const focused = document.activeElement;
  const focusId = focused?.dataset.taskId || focused?.dataset.menuFor;
  const focusMenu = Boolean(focused?.dataset.menuFor);
  list.replaceChildren();
  const pinned = state.chats.filter((chat) => chat.pinned);
  const groups = pinned.length
    ? [
        ["Pinned", pinned],
        ["Recent", state.chats.filter((chat) => !chat.pinned)],
      ]
    : [["", state.chats]];
  for (const [label, chats] of groups) {
    if (label && chats.length) list.append(textNode("p", label, "list-group"));
    for (const chat of chats) list.append(renderRow(chat));
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
  $("list-empty").hidden = state.chats.length > 0;
  $("load-more").hidden = state.next === null;
}
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
      exchange.append(textNode("div", turn.message, "message user"));
    const attachments = imageAttachments(turn.attachments);
    const hasAnswer =
      turn.summary || turn.details || turn.question || attachments.length;
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
  if (!state.chatSettings.has(id))
    state.chatSettings.set(
      id,
      data.task.chat_settings || { model: null, reasoning_effort: null },
    );
  state.loading = false;
  renderTask(force);
}
async function selectChat(id) {
  if (state.busy || state.settingsBusy) return;
  closePicker();
  state.drafts.set(state.id, $("message").value);
  state.draftFiles.set(state.id, state.files);
  state.id = id;
  state.files = state.draftFiles.get(id) || [];
  renderPending();
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
      },
    );
    state.drafts.delete(id);
    state.draftFiles.delete(id);
    state.chatSettings.delete(id);
    clearUploads();
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
      if (!state.catalog) await loadChatOptions();
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
