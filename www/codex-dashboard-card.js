const CODEX_CONVERSATION_INSTRUCTION = "Ongoing conversation. After each turn return needs_input. Return completed only when I explicitly ask to end the conversation.";

const CODEX_END_CONVERSATION_INSTRUCTION = 'End this conversation now. Return status "completed" and do not ask for further input.';
const DASHBOARD_METADATA_PATTERN = /\n\n\[codex-dashboard(?: conversation=[A-Za-z0-9_-]+)?(?: parent=[A-Za-z0-9_-]+)?(?: request_chars=(\d+))?\]\s*$/;
const FRESH_CONTEXT_MARKER = "Context from the previous task:";
const LEGACY_CONVERSATION_MARKER = "\n\nTreat this task as an ongoing conversation.";
const PREVIOUS_QUESTION_LIMIT = 1000;
const PREVIOUS_REQUEST_LIMIT = 3000;
const PREVIOUS_SUMMARY_LIMIT = 2000;
const PREVIOUS_DETAILS_LIMIT = 1500;
const REQUEST_COLLAPSE_CHARACTER_LIMIT = 360;
const REQUEST_COLLAPSE_LINE_LIMIT = 6;

class CodexDashboardCard extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._tasks = [];
    this._selectedId = localStorage.getItem("codex-dashboard-task-id") || "";
    this._newConversation = !this._selectedId;
    this._contextMode = "compact";
    this._busy = false;
    this._message = "";
    this._messageTimer = null;
    this._unsub = null;
    this._subscribing = false;
    this._connected = false;
    this._subscriptionGeneration = 0;
    this._displayedTaskState = "";
    this._requestViewKey = "";
    this._requestExpanded = false;
    this._pendingRequest = null;
    this._markdownHelpersPromise = null;
    this._markdownHelpers = null;
    this._markdownUnavailable = false;
    this._renderShell();
  }

  setConfig() {}

  set hass(hass) {
    const firstSet = !this._hass;
    this._hass = hass;
    this._setMarkdownHass();
    if (this._connected) {
      this._subscribe();
      if (firstSet) this._refreshTasks();
    }
  }

  connectedCallback() {
    this._connected = true;
    if (!this._hass) return;
    this._subscribe();
    this._refreshTasks();
  }

  disconnectedCallback() {
    this._connected = false;
    this._subscriptionGeneration += 1;
    if (this._unsub) this._unsub();
    this._unsub = null;
    if (this._messageTimer) clearTimeout(this._messageTimer);
  }

  getCardSize() {
    return 8;
  }

  _renderShell() {
    this.shadowRoot.innerHTML = `
      <style>
        :host {
          display: block; max-width: 920px; margin: 20px auto;
          font-family: var(--ha-font-family-body, inherit);
          font-size: var(--ha-font-size-m, 14px);
          line-height: var(--ha-line-height-normal, 1.45);
        }
        ha-card { padding: 18px; overflow: hidden; }
        .top { display: flex; align-items: center; gap: 12px; margin-bottom: 16px; }
        .logo { display: grid; place-items: center; width: 42px; height: 42px; border-radius: 14px;
          background: var(--primary-color); color: var(--text-primary-color); font-size: var(--ha-font-size-xl, 23px); }
        .heading { min-width: 0; }
        h1 { font-family: var(--ha-font-family-heading, var(--ha-font-family-body, inherit)); font-size: var(--ha-font-size-xl, 22px);
          font-weight: var(--ha-font-weight-bold, 700); line-height: var(--ha-line-height-condensed, 1.15); margin: 0; }
        .subtitle { color: var(--secondary-text-color); font-size: var(--ha-font-size-s, 13px); margin-top: 3px; }
        .global-state { margin-left: auto; border-radius: 999px; padding: .45em .82em; font-size: var(--ha-font-size-s, 11px);
          font-weight: var(--ha-font-weight-bold, 700);
          color: var(--secondary-text-color); background: var(--secondary-background-color); }
        .global-state.running { color: var(--warning-color); }
        .header-new { min-height: 2.6em; padding: .36em .64em; white-space: nowrap; }
        .selectors { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
        label { display: block; color: var(--secondary-text-color); font-size: var(--ha-font-size-s, 12px);
          font-weight: var(--ha-font-weight-medium, 600); margin: 0 0 5px 2px; }
        select, textarea { box-sizing: border-box; width: 100%; border: 1px solid var(--divider-color);
          border-radius: 12px; background: var(--card-background-color); color: var(--primary-text-color);
          font: inherit; padding: .78em .86em; }
        select { min-height: 3.15em; text-overflow: ellipsis; }
        textarea { min-height: 8.86em; resize: vertical; line-height: var(--ha-line-height-normal, 1.45); }
        select:focus, textarea:focus { outline: 2px solid var(--primary-color); outline-offset: 1px; }
        .actions { display: flex; justify-content: flex-end; gap: 9px; margin-top: 10px; }
        button { min-height: 3.15em; border: 0; border-radius: 12px; padding: .57em .72em; font: inherit;
          font-weight: var(--ha-font-weight-medium, 600); cursor: pointer; color: var(--primary-text-color); background: var(--secondary-background-color); }
        button.primary { background: var(--primary-color); color: var(--text-primary-color); }
        button.danger { color: var(--error-color); }
        button:disabled { cursor: default; opacity: .45; }
        .message { display: none; margin: 11px 0; border-radius: 10px; padding: .69em .85em;
          background: color-mix(in srgb, var(--primary-color) 12%, transparent); font-size: var(--ha-font-size-s, 13px); }
        .message.show { display: block; }
        .busy-notice { display: none; align-items: center; justify-content: space-between; gap: 10px; margin-top: 12px;
          border-radius: 10px; padding: .69em .85em; background: color-mix(in srgb, var(--warning-color) 14%, transparent);
          color: var(--primary-text-color); font-size: var(--ha-font-size-s, 13px); }
        .busy-notice.show { display: flex; }
        .busy-notice button { min-height: 2.6em; flex: 0 0 auto; padding: .38em .77em; }
        .task-overview { margin-top: 16px; }
        .status-line { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
        .status { border-radius: 999px; padding: .42em .84em; font-size: var(--ha-font-size-s, 12px);
          font-weight: var(--ha-font-weight-bold, 700); text-transform: uppercase;
          letter-spacing: .03em; background: var(--secondary-background-color); }
        .status.running { color: var(--warning-color); }
        .status.waiting_for_input { color: var(--info-color, var(--primary-color)); }
        .status.completed { color: var(--success-color); }
        .status.failed, .status.cancelled { color: var(--error-color); }
        .task-id { color: var(--secondary-text-color); font-family: var(--code-font-family, monospace);
          font-size: var(--ha-font-size-s, 12px); overflow-wrap: anywhere; }
        .request { margin-top: 16px; }
        .request-box { --request-background: color-mix(in srgb, var(--warning-color, var(--primary-color)) 7%, var(--card-background-color));
          border: 1px solid color-mix(in srgb, var(--warning-color, var(--primary-color)) 16%, var(--divider-color));
          border-radius: 12px; padding: 12px; background: var(--request-background); overflow: hidden; }
        .request-text-wrap { position: relative; }
        .request-text-wrap.collapsed { max-height: 7.25em; overflow: hidden; }
        .request-text-wrap.collapsed::after { position: absolute; right: 0; bottom: 0; left: 0; height: 2.1em;
          background: linear-gradient(to bottom, transparent, var(--request-background)); content: ""; pointer-events: none; }
        .request-value { font-size: var(--ha-font-size-m, 14px); white-space: pre-wrap; overflow-wrap: anywhere;
          line-height: var(--ha-line-height-normal, 1.45);
          user-select: text; -webkit-user-select: text; cursor: text; }
        .request-toggle { display: block; min-height: 2.6em; margin: 7px 0 -5px auto; padding: .34em .58em;
          color: var(--primary-color); background: transparent; font-size: var(--ha-font-size-s, 12px); }
        .working { margin-top: 14px; color: var(--secondary-text-color); font-size: var(--ha-font-size-s, 13px); }
        .response { margin-top: 18px; }
        .response-heading { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
        .response-heading h2 { margin: 0; color: var(--secondary-text-color); font-size: var(--ha-font-size-s, 11px);
          font-weight: var(--ha-font-weight-bold, 700);
          letter-spacing: .05em; text-transform: uppercase; }
        .copy { min-height: 2.6em; padding: .42em .75em; color: var(--secondary-text-color); font-size: var(--ha-font-size-s, 12px); }
        #response-content { margin-top: 5px; border: 1px solid color-mix(in srgb, var(--primary-color) 16%, var(--divider-color));
          border-radius: 12px; padding: 12px; background: color-mix(in srgb, var(--primary-color) 7%, var(--card-background-color)); }
        #response-content .field:first-child { margin-top: 0; }
        .field { margin-top: 14px; }
        .field-name { color: var(--secondary-text-color); font-size: var(--ha-font-size-s, 11px);
          font-weight: var(--ha-font-weight-bold, 700); letter-spacing: .05em;
          text-transform: uppercase; margin-bottom: 4px; }
        .markdown-content { min-width: 0; max-width: 100%; overflow-x: auto; font-size: var(--ha-font-size-m, 14px);
          line-height: var(--ha-line-height-normal, 1.45); overflow-wrap: anywhere;
          user-select: text; -webkit-user-select: text; cursor: text; }
        .markdown-content > * { max-width: 100%; }
        .markdown-content hui-markdown-card { display: block; max-width: 100%; }
        .markdown-fallback { white-space: pre-wrap; overflow-wrap: anywhere; }
        .composer { margin-top: 18px; border-top: 1px solid var(--divider-color); padding-top: 16px; }
        .question { margin: 0 0 12px; }
        .question-speaker { font-size: var(--ha-font-size-s, 13px); font-weight: var(--ha-font-weight-bold, 700); margin-bottom: 4px; }
        .question-text { font-size: var(--ha-font-size-m, 14px); }
        .context-controls { display: flex; align-items: flex-end; justify-content: space-between; gap: 12px; margin-top: 10px; }
        .context-field { flex: 1 1 auto; min-width: 0; }
        .context-help, .context-description { color: var(--secondary-text-color); font-size: var(--ha-font-size-s, 12px);
          line-height: var(--ha-line-height-condensed, 1.35); }
        .context-help { margin: 0 0 7px 2px; }
        .context-description { margin: 6px 0 0 2px; }
        .context-field select { max-width: 220px; }
        .empty { color: var(--secondary-text-color); font-size: var(--ha-font-size-m, 14px); padding: 18px 0 4px; text-align: center; }
        [hidden] { display: none !important; }
        @media (max-width: 600px) {
          :host { margin: 0; }
          ha-card { padding: 14px; }
          .selectors { grid-template-columns: 1fr; gap: 9px; }
          .actions { justify-content: stretch; }
          .actions button { flex: 1 1 auto; }
          .actions button.primary { order: 2; }
          .top { align-items: flex-start; flex-wrap: wrap; }
          .global-state { margin-left: auto; }
          .header-new { margin-left: auto; }
          .context-controls { align-items: stretch; flex-direction: column; }
          .context-field select { max-width: none; }
          .context-controls #send { width: 100%; }
        }
      </style>
      <ha-card>
        <div class="top">
          <div class="logo">⌁</div>
          <div class="heading"><h1>Codex</h1><div class="subtitle">Home Assistant tasks and conversations</div></div>
          <span class="global-state" id="global-state">Available</span>
          <button class="header-new" id="new">+ New Conversation</button>
        </div>
        <div class="selectors">
          <div><label for="all-tasks">All Tasks</label><select id="all-tasks"></select></div>
          <div><label for="waiting-tasks">Waiting for Input</label><select id="waiting-tasks"></select></div>
        </div>
        <div class="task-overview" id="task-overview"></div>
        <div class="busy-notice" id="busy-notice"><span id="busy-text"></span><button id="view-running">View task</button></div>
        <div class="request" id="request">
          <div class="field-name" id="request-label"></div>
          <div class="request-box">
            <div class="request-text-wrap" id="request-text-wrap"><div class="request-value" id="request-value"></div></div>
            <button class="request-toggle" id="request-toggle" aria-controls="request-value">Show full request</button>
          </div>
        </div>
        <div class="working" id="working">Codex is working…</div>
        <div class="response" id="response">
          <div class="response-heading"><h2>Codex response</h2><button class="copy" id="copy-response">Copy response</button></div>
          <div id="response-content"></div>
        </div>
        <div class="composer" id="composer">
          <div class="question" id="question"><div class="question-speaker">Codex</div><div class="question-text" id="question-text"></div></div>
          <textarea id="instruction" placeholder="What would you like Codex to do?"></textarea>
          <div class="context-controls">
            <div class="context-field">
              <label for="conversation-context">Conversation context</label>
              <div class="context-help">How much of this conversation should Codex carry forward to the next request?</div>
              <select id="conversation-context">
                <option value="none">None</option>
                <option value="compact">Compact</option>
                <option value="full">Full</option>
              </select>
              <div class="context-description" id="context-description"></div>
            </div>
            <button class="primary" id="send">Send</button>
          </div>
          <div class="actions"><button class="danger" id="end">End Conversation</button></div>
        </div>
        <div class="actions" id="task-actions">
          <button class="danger" id="cancel">Cancel Task</button>
        </div>
        <div class="message" id="message"></div>
      </ha-card>`;
    this.shadowRoot.getElementById("all-tasks").addEventListener("change", (event) => this._selectTask(event.target.value));
    this.shadowRoot.getElementById("waiting-tasks").addEventListener("change", (event) => this._selectTask(event.target.value));
    this.shadowRoot.getElementById("send").addEventListener("click", () => this._send());
    this.shadowRoot.getElementById("new").addEventListener("click", () => this._startNew());
    this.shadowRoot.getElementById("cancel").addEventListener("click", () => this._cancel());
    this.shadowRoot.getElementById("end").addEventListener("click", () => this._endConversation());
    this.shadowRoot.getElementById("copy-response").addEventListener("click", () => this._copyResponse());
    this.shadowRoot.getElementById("request-toggle").addEventListener("click", () => this._toggleRequestExpansion());
    this.shadowRoot.getElementById("conversation-context").addEventListener("change", (event) => this._setContextMode(event.target.value));
    this.shadowRoot.getElementById("view-running").addEventListener("click", () => {
      const task = this._runningTask();
      if (task) this._selectTask(task.task_id);
    });
    this.shadowRoot.getElementById("instruction").addEventListener("input", () => this._updateControls());
    this._updateView();
  }

  async _call(service, serviceData = {}) {
    const raw = await this._hass.callWS({
      type: "call_service", domain: "codex_cli", service,
      service_data: serviceData, return_response: true,
    });
    const result = raw?.response ?? raw?.service_response ?? raw ?? {};
    if (result?.ok === false) throw new Error(this._errorText(result));
    return result;
  }

  async _subscribe() {
    if (!this._connected || !this._hass || this._unsub || this._subscribing) return;
    const generation = this._subscriptionGeneration;
    this._subscribing = true;
    try {
      const unsub = await this._hass.connection.subscribeEvents((event) => this._handleResultEvent(event.data || {}), "codex_cli_task_result");
      if (!this._connected || generation !== this._subscriptionGeneration) unsub();
      else this._unsub = unsub;
    } catch (error) {
      this._setMessage(`Could not subscribe to Codex results: ${this._errorText(error)}`);
    } finally {
      this._subscribing = false;
      if (this._connected && generation !== this._subscriptionGeneration && !this._unsub) this._subscribe();
    }
  }

  async _refreshTasks(preferNewest = false) {
    if (!this._hass) return;
    try {
      const result = await this._call("list_tasks");
      const previousSelectedId = this._selectedId;
      const existing = new Map(this._tasks.map((task) => [task.task_id, task]));
      const selectedWasLoaded = existing.has(previousSelectedId);
      const listedTasks = Array.isArray(result.tasks)
        ? result.tasks.map((task) => ({ ...(existing.get(task.task_id) || {}), ...task, _optimistic: false }))
        : [];
      const optimisticSelected = existing.get(this._selectedId);
      if (optimisticSelected?._optimistic && !listedTasks.some((task) => task.task_id === optimisticSelected.task_id)) {
        listedTasks.push(optimisticSelected);
      }
      this._tasks = listedTasks.sort((a, b) => String(b.created_at || "").localeCompare(String(a.created_at || "")));
      if (preferNewest && this._tasks.length) this._setSelectedId(this._tasks[0].task_id);
      if (this._selectedId && !this._tasks.some((task) => task.task_id === this._selectedId)) this._setSelectedId("");
      const selected = this._selectedTask();
      if (selected && (!selectedWasLoaded || this._selectedId !== previousSelectedId)) {
        this._contextMode = this._defaultContextMode(selected);
      }
      this._updateView();
      if (selected && !selected._detailsLoaded && !selected._optimistic) {
        const detailResult = await this._call("get_task", { task_id: selected.task_id });
        if (detailResult.task) this._mergeTask({ ...detailResult.task, _detailsLoaded: true });
      }
    } catch (error) {
      this._setMessage(`Could not load tasks: ${this._errorText(error)}`);
    }
  }

  async _selectTask(taskId) {
    if (!taskId) return;
    this._newConversation = false;
    this._setSelectedId(taskId);
    this._contextMode = this._defaultContextMode(this._selectedTask());
    this._updateView();
    try {
      const result = await this._call("get_task", { task_id: taskId });
      if (result.task) this._mergeTask({ ...result.task, _detailsLoaded: true });
    } catch (error) {
      this._setMessage(`Could not load the selected task: ${this._errorText(error)}`);
    }
  }

  async _send() {
    const textarea = this.shadowRoot.getElementById("instruction");
    const instruction = textarea.value.trim();
    if (!instruction || this._busy) return;
    const task = this._selectedTask();
    const runningTask = this._runningTask();
    if (runningTask) {
      this._setMessage(this._busyText(runningTask));
      return;
    }
    this._busy = true;
    const startingNew = this._newConversation;
    this._setMessage(startingNew ? "Starting task…" : "Sending request…");
    this._updateControls();
    try {
      let result;
      if (this._newConversation) {
        const prompt = this._buildContextlessPrompt(instruction);
        result = await this._call("start_task", { prompt });
        const taskId = result.task_id;
        this._newConversation = false;
        this._contextMode = "full";
        this._setSelectedId(taskId);
        this._mergeTask({ task_id: taskId, title: instruction.slice(0, 80), prompt, status: result.status || "queued", created_at: new Date().toISOString(), summary: "", details: "", question: "", _optimistic: true });
      } else if (task && this._contextMode === "full") {
        result = await this._sendFull(instruction, task);
      } else if (task) {
        const prompt = this._contextMode === "none"
          ? this._buildContextlessPrompt(instruction)
          : this._buildCompactPrompt(instruction, task);
        result = await this._call("start_task", { prompt });
        const taskId = result.task_id;
        this._setSelectedId(taskId);
        this._mergeTask({ task_id: taskId, title: instruction.slice(0, 80), prompt, status: result.status || "queued", created_at: new Date().toISOString(), summary: "", details: "", question: "", _optimistic: true });
        this._contextMode = "full";
      } else {
        throw new Error("Select a conversation, or choose New Conversation.");
      }
      textarea.value = "";
      this._setMessage(startingNew ? "Conversation started." : "Request sent.");
      this._updateView();
      await this._refreshTasks();
    } catch (error) {
      this._setMessage(this._errorText(error));
    } finally {
      this._busy = false;
      this._updateControls();
    }
  }

  _startNew() {
    const runningTask = this._runningTask();
    if (runningTask) {
      this._setMessage(this._busyText(runningTask));
      return;
    }
    this._newConversation = true;
    this._contextMode = "none";
    this._setSelectedId("");
    this.shadowRoot.getElementById("instruction").value = "";
    this._updateView();
    this._setMessage("New conversation ready. Enter a request and press Send.");
    this.shadowRoot.getElementById("instruction").focus();
  }

  async _sendFull(instruction, task) {
    if (!this._canUseFull(task)) throw new Error("Full context is unavailable for this task.");
    this._pendingRequest = { taskId: task.task_id, value: instruction };
    try {
      const result = await this._call("reply_task", { task_id: task.task_id, reply: instruction });
      this._setSelectedId(task.task_id);
      this._mergeTask({ task_id: task.task_id, status: result.status || "queued", summary: "", details: "", question: "" });
      return result;
    } catch (error) {
      if (this._pendingRequest?.taskId === task.task_id) this._pendingRequest = null;
      throw error;
    }
  }

  async _cancel() {
    const task = this._selectedTask();
    const runningTask = this._runningTask();
    if (!task || runningTask?.task_id !== task.task_id || this._busy) return;
    if (!window.confirm(`Cancel the running Codex task “${task.title || task.task_id}”?`)) return;
    this._busy = true;
    this._setMessage("Cancelling…");
    this._updateControls();
    try {
      const result = await this._call("cancel_task", { task_id: task.task_id });
      this._mergeTask({ ...task, status: result.status || "cancelled", summary: "", details: "", question: "" });
      this._setMessage("Cancellation requested.");
      await this._refreshTasks();
    } catch (error) {
      this._setMessage(this._errorText(error));
    } finally {
      this._busy = false;
      this._updateControls();
    }
  }

  async _endConversation() {
    const task = this._selectedTask();
    if (!task || task.status !== "waiting_for_input" || this._busy) return;
    const runningTask = this._runningTask();
    if (runningTask) {
      this._setMessage(this._busyText(runningTask));
      return;
    }
    if (!window.confirm(`End the Codex conversation “${task.title || task.task_id}”? Codex will receive an instruction to end it.`)) return;
    this._busy = true;
    this._setMessage("Ending conversation…");
    this._updateControls();
    try {
      const result = await this._call("reply_task", {
        task_id: task.task_id,
        reply: CODEX_END_CONVERSATION_INSTRUCTION,
      });
      this._mergeTask({ task_id: task.task_id, status: result.status || "queued", summary: "", details: "", question: "" });
      this._setMessage("Closing request sent.");
      await this._refreshTasks();
    } catch (error) {
      this._setMessage(this._errorText(error));
    } finally {
      this._busy = false;
      this._updateControls();
    }
  }

  async _handleResultEvent(data) {
    if (!data.task_id) return;
    if (this._pendingRequest?.taskId === data.task_id) this._pendingRequest = null;
    this._mergeTask(data);
    await this._refreshTasks();
  }

  _mergeTask(update) {
    const index = this._tasks.findIndex((task) => task.task_id === update.task_id);
    if (index >= 0) this._tasks[index] = { ...this._tasks[index], ...update };
    else this._tasks.unshift(update);
    this._updateView();
  }

  _setSelectedId(taskId) {
    this._selectedId = taskId || "";
    if (this._selectedId) localStorage.setItem("codex-dashboard-task-id", this._selectedId);
    else localStorage.removeItem("codex-dashboard-task-id");
  }

  _selectedTask() {
    return this._tasks.find((task) => task.task_id === this._selectedId);
  }

  _runningTask() {
    return this._tasks.find((task) => this._isRunning(task.status));
  }

  _currentRequestForTask(task) {
    if (!task) return "";
    if (this._pendingRequest && this._pendingRequest.taskId === task.task_id) return this._pendingRequest.value;
    const replies = (Array.isArray(task.reply_history) ? task.reply_history : [])
      .map((entry) => String(typeof entry === "string" ? entry : entry?.reply || "").trim())
      .filter((reply) => reply && reply !== CODEX_END_CONVERSATION_INSTRUCTION);
    if (replies.length) return replies[replies.length - 1];
    const metadata = this._metadataForTask(task);
    const prompt = this._withoutMetadata(task.prompt);
    if (metadata && Number.isInteger(metadata.requestChars)) return prompt.slice(0, metadata.requestChars).trim();
    const suffix = CODEX_CONVERSATION_INSTRUCTION.trim();
    if (prompt.endsWith(suffix)) return prompt.slice(0, -suffix.length).trim();
    const legacyMarker = prompt.lastIndexOf(LEGACY_CONVERSATION_MARKER);
    return legacyMarker >= 0 ? prompt.slice(0, legacyMarker).trim() : prompt;
  }

  _setContextMode(mode) {
    if (this._newConversation || !["none", "compact", "full"].includes(mode)) return;
    this._contextMode = mode;
    this._updateControls();
  }

  _canUseFull(task = this._selectedTask()) {
    return Boolean(task && task.status === "waiting_for_input");
  }

  _defaultContextMode(task = this._selectedTask()) {
    return this._canUseFull(task) ? "full" : "compact";
  }

  _metadataForTask(task) {
    const match = String(task?.prompt || "").match(DASHBOARD_METADATA_PATTERN);
    if (!match) return null;
    return {
      requestChars: match[1] === undefined ? null : Number(match[1]),
    };
  }

  _withMetadata(prompt, requestChars) {
    return `${prompt}\n\n[codex-dashboard request_chars=${requestChars}]`;
  }

  _withoutMetadata(prompt) {
    return String(prompt || "").replace(DASHBOARD_METADATA_PATTERN, "").trim();
  }

  _bounded(value, limit) {
    const text = String(value || "").trim();
    if (text.length <= limit) return text;
    return `${text.slice(0, Math.max(0, limit - 1)).trimEnd()}…`;
  }

  _buildContextlessPrompt(instruction) {
    return this._withMetadata(`${instruction}\n\n${CODEX_CONVERSATION_INSTRUCTION}`, instruction.length);
  }

  _buildCompactPrompt(instruction, task) {
    const previousQuestion = this._bounded(task?.question, PREVIOUS_QUESTION_LIMIT);
    const previousRequest = this._bounded(this._currentRequestForTask(task), PREVIOUS_REQUEST_LIMIT) || "Not available.";
    const previousSummary = this._bounded(task?.summary, PREVIOUS_SUMMARY_LIMIT) || "Not available.";
    const previousDetails = this._bounded(task?.details || task?.error, PREVIOUS_DETAILS_LIMIT);
    const handoff = [FRESH_CONTEXT_MARKER];
    if (previousQuestion) handoff.push(`Codex asked:\n${previousQuestion}`);
    handoff.push(`Previous request:\n${previousRequest}`, `Previous result:\n${previousSummary}`);
    if (previousDetails) handoff.push(`Relevant details:\n${previousDetails}`);
    const parts = [instruction, handoff.join("\n\n")];
    parts.push("Use live `/config` and Home Assistant runtime as current authority.", CODEX_CONVERSATION_INSTRUCTION);
    return this._withMetadata(parts.join("\n\n"), instruction.length);
  }

  _busyText(task) {
    const title = String(task?.title || task?.task_id || "another task").replace(/\s+/g, " ").trim();
    return `Codex is busy with “${title.length > 64 ? `${title.slice(0, 63)}…` : title}”.`;
  }

  _taskLabel(task, includeStatus = false) {
    const title = String(task.title || "Untitled task").replace(/\s+/g, " ").trim();
    const prefix = includeStatus ? `[${this._statusLabel(task.status)}] ` : "";
    return `${prefix}${title.length > 48 ? `${title.slice(0, 47)}…` : title} · ${task.task_id}`;
  }

  _statusLabel(status) {
    const labels = { queued: "Running", running: "Running", cancelling: "Running", waiting_for_input: "Waiting for input", completed: "Completed", failed: "Failed", cancelled: "Cancelled" };
    return labels[status] || String(status || "Unknown").replaceAll("_", " ");
  }

  _statusClass(status) {
    return this._isRunning(status) ? "running" : (status || "unknown");
  }

  _isRunning(status) {
    return ["queued", "running", "cancelling"].includes(status);
  }

  _isLongRequest(requestText) {
    return Array.from(requestText).length > REQUEST_COLLAPSE_CHARACTER_LIMIT
      || requestText.split(/\r?\n/).length > REQUEST_COLLAPSE_LINE_LIMIT;
  }

  _updateRequestExpansion(isLong) {
    const collapsed = isLong && !this._requestExpanded;
    this.shadowRoot.getElementById("request-text-wrap").classList.toggle("collapsed", collapsed);
    const toggle = this.shadowRoot.getElementById("request-toggle");
    toggle.hidden = !isLong;
    toggle.textContent = this._requestExpanded ? "Collapse request" : "Show full request";
    toggle.setAttribute("aria-expanded", String(isLong && this._requestExpanded));
  }

  _toggleRequestExpansion() {
    const requestText = this.shadowRoot.getElementById("request-value").textContent || "";
    if (!this._isLongRequest(requestText)) return;
    this._requestExpanded = !this._requestExpanded;
    this._updateRequestExpansion(true);
  }

  _updateView() {
    const all = this.shadowRoot.getElementById("all-tasks");
    const waiting = this.shadowRoot.getElementById("waiting-tasks");
    const fill = (select, tasks, placeholder, includeStatus = false) => {
      select.replaceChildren();
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = placeholder;
      select.appendChild(empty);
      for (const task of tasks) {
        const option = document.createElement("option");
        option.value = task.task_id;
        option.textContent = this._taskLabel(task, includeStatus);
        select.appendChild(option);
      }
    };
    fill(all, this._tasks, this._newConversation ? "New conversation" : "Select a task", true);
    fill(waiting, this._tasks.filter((task) => task.status === "waiting_for_input"), "Select a waiting task");
    all.value = this._selectedId;
    const selectedForWaiting = this._tasks.find((task) => task.task_id === this._selectedId);
    waiting.value = selectedForWaiting?.status === "waiting_for_input" ? this._selectedId : "";

    const task = this._selectedTask();
    const runningTask = this._runningTask();
    const taskState = this._newConversation ? "new" : `${task?.task_id || "none"}:${task?.status || ""}`;
    if (this._displayedTaskState && this._displayedTaskState !== taskState) this._setMessage("");
    this._displayedTaskState = taskState;

    const globalState = this.shadowRoot.getElementById("global-state");
    globalState.textContent = runningTask ? "Running" : "Available";
    globalState.classList.toggle("running", Boolean(runningTask));
    const notice = this.shadowRoot.getElementById("busy-notice");
    this.shadowRoot.getElementById("busy-text").textContent = runningTask ? this._busyText(runningTask) : "";
    this.shadowRoot.getElementById("view-running").hidden = !runningTask || runningTask.task_id === this._selectedId;
    notice.classList.toggle("show", Boolean(runningTask && runningTask.task_id !== this._selectedId));

    const overview = this.shadowRoot.getElementById("task-overview");
    const request = this.shadowRoot.getElementById("request");
    const response = this.shadowRoot.getElementById("response");
    const responseContent = this.shadowRoot.getElementById("response-content");
    overview.replaceChildren();
    if (this._newConversation) {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = "Start a new conversation.";
      overview.appendChild(empty);
      this._syncResponseFields(responseContent, null, false);
    } else if (task) {
      const statusLine = document.createElement("div");
      statusLine.className = "status-line";
      const status = document.createElement("span");
      status.className = `status ${this._statusClass(task.status)}`;
      status.textContent = this._statusLabel(task.status);
      const id = document.createElement("span");
      id.className = "task-id";
      id.textContent = task.task_id;
      statusLine.append(status, id);
      overview.appendChild(statusLine);
      this._syncResponseFields(responseContent, task, this._isRunning(task.status));
    } else {
      const empty = document.createElement("div");
      empty.className = "empty";
      empty.textContent = this._tasks.length ? "Select a task." : "Loading tasks…";
      overview.appendChild(empty);
      this._syncResponseFields(responseContent, null, false);
    }
    const selectedRunning = Boolean(task && this._isRunning(task.status));
    const requestText = this._currentRequestForTask(task);
    const requestViewKey = requestText ? `${task?.task_id || ""}:${requestText}` : "";
    if (requestViewKey !== this._requestViewKey) {
      this._requestViewKey = requestViewKey;
      this._requestExpanded = false;
    }
    request.hidden = !requestText;
    this.shadowRoot.getElementById("request-label").textContent = selectedRunning ? "Working on" : "Request";
    this.shadowRoot.getElementById("request-value").textContent = requestText;
    this._updateRequestExpansion(this._isLongRequest(requestText));
    this.shadowRoot.getElementById("working").hidden = !selectedRunning;

    const hasResponse = Boolean(task && !selectedRunning && (String(task.summary || "").trim() || String(task.details || task.error || "").trim()));
    response.hidden = !hasResponse;
    this.shadowRoot.getElementById("copy-response").hidden = !hasResponse;

    const canCompose = !runningTask && (this._newConversation || Boolean(task && !this._isRunning(task.status)));
    const composer = this.shadowRoot.getElementById("composer");
    composer.hidden = !canCompose;
    const question = this.shadowRoot.getElementById("question");
    const questionText = String(task?.question || "").trim();
    question.hidden = !questionText;
    this._renderMarkdown(this.shadowRoot.getElementById("question-text"), questionText);
    this.shadowRoot.getElementById("instruction").placeholder = this._newConversation
      ? "What would you like Codex to do?"
      : "What would you like Codex to do next?";
    this._updateControls();
  }

  _syncResponseFields(parent, task, selectedRunning) {
    const fields = [];
    if (task && !selectedRunning && String(task.summary || "").trim()) {
      fields.push({ name: "Summary", value: String(task.summary), className: "summary" });
    }
    const details = String(task?.details || task?.error || "").trim();
    if (task && !selectedRunning && details) {
      fields.push({ name: "Details", value: details, className: "details" });
    }
    const key = fields.map((field) => `${field.className}\u0000${field.value}`).join("\u0001");
    if (parent.dataset.renderKey === key) {
      this._setMarkdownHass(parent);
      this._upgradeMarkdownFallbacks(parent);
      return;
    }
    parent.dataset.renderKey = key;
    parent.replaceChildren();
    for (const field of fields) this._appendMarkdownField(parent, field.name, field.value, field.className);
  }

  _appendMarkdownField(parent, name, value, className) {
    const field = document.createElement("div");
    field.className = `field ${className || ""}`;
    if (name) {
      const heading = document.createElement("div");
      heading.className = "field-name";
      heading.textContent = name;
      field.appendChild(heading);
    }
    const content = document.createElement("div");
    content.className = "field-value markdown-content";
    this._renderMarkdown(content, String(value));
    field.appendChild(content);
    parent.appendChild(field);
  }

  _markdownCardConfig(content) {
    return { type: "markdown", content, text_only: true };
  }

  _renderMarkdown(container, value) {
    const content = String(value || "").trim();
    container.classList.add("markdown-content");
    container.dataset.markdownSource = content;
    if (!content) {
      container.replaceChildren();
      container._markdownCard = null;
      return;
    }
    if (container._markdownCard && container._markdownSource === content) {
      container._markdownCard.hass = this._hass;
      return;
    }

    const fallback = document.createElement("div");
    fallback.className = "markdown-fallback";
    fallback.textContent = content;
    container.replaceChildren(fallback);
    container._markdownCard = null;
    container._markdownSource = content;
    this._loadMarkdownCard(container, content);
  }

  async _loadMarkdownCard(container, content) {
    const helpers = await this._getCardHelpers();
    if (!helpers || container.dataset.markdownSource !== content) return;
    try {
      const config = this._markdownCardConfig(content);
      const card = await helpers.createCardElement(config);
      if (container.dataset.markdownSource !== content) return;
      card.classList.add("markdown-card");
      card.hass = this._hass;
      container._markdownCard = card;
      container._markdownSource = content;
      container.replaceChildren(card);
    } catch (error) {
      container._markdownCard = null;
    }
  }

  async _getCardHelpers() {
    if (this._markdownHelpers) return this._markdownHelpers;
    if (this._markdownUnavailable) return null;
    if (!this._markdownHelpersPromise) {
      if (typeof window.loadCardHelpers !== "function") {
        return null;
      }
      this._markdownHelpersPromise = window.loadCardHelpers()
        .then((helpers) => {
          this._markdownHelpers = helpers;
          return helpers;
        })
        .catch(() => {
          this._markdownUnavailable = true;
          return null;
        });
    }
    return this._markdownHelpersPromise;
  }

  _setMarkdownHass(root = this.shadowRoot) {
    if (!root || !this._hass) return;
    root.querySelectorAll(".markdown-card").forEach((card) => {
      card.hass = this._hass;
    });
  }

  _upgradeMarkdownFallbacks(root = this.shadowRoot) {
    if (!root) return;
    root.querySelectorAll(".markdown-content").forEach((container) => {
      if (!container._markdownCard && container.dataset.markdownSource) {
        this._loadMarkdownCard(container, container.dataset.markdownSource);
      }
    });
  }

  _updateControls() {
    const task = this._selectedTask();
    const runningTask = this._runningTask();
    const hasText = Boolean(this.shadowRoot.getElementById("instruction").value.trim());
    const canContinue = Boolean(!runningTask && task && !this._isRunning(task.status));
    const canSend = !runningTask && (this._newConversation || canContinue);
    const send = this.shadowRoot.getElementById("send");
    const startNew = this.shadowRoot.getElementById("new");
    const cancel = this.shadowRoot.getElementById("cancel");
    const end = this.shadowRoot.getElementById("end");
    const context = this.shadowRoot.getElementById("conversation-context");
    const fullOption = context.querySelector('option[value="full"]');
    const fullAvailable = this._canUseFull(task);
    if (this._newConversation) this._contextMode = "none";
    if (this._contextMode === "full" && !fullAvailable && !this._isRunning(task?.status)) this._contextMode = "compact";
    context.value = this._contextMode;
    context.disabled = this._busy || this._newConversation || Boolean(runningTask);
    fullOption.disabled = !fullAvailable;
    const descriptions = {
      none: "Use only the new request.",
      compact: "Carry forward the essential conversation context.",
      full: "Recommended · carry forward the complete current Codex session context.",
    };
    this.shadowRoot.getElementById("context-description").textContent = !this._newConversation && !fullAvailable && this._contextMode !== "full"
      ? `${descriptions[this._contextMode]} Full context is unavailable for this task.`
      : descriptions[this._contextMode];
    send.hidden = !canSend;
    send.textContent = "Send";
    send.disabled = this._busy || !hasText || !canSend;
    end.hidden = task?.status !== "waiting_for_input" || Boolean(runningTask);
    end.disabled = this._busy || end.hidden;
    cancel.hidden = !task || runningTask?.task_id !== task.task_id;
    cancel.disabled = this._busy || !task || runningTask?.task_id !== task.task_id;
    startNew.disabled = this._busy || Boolean(runningTask);
    this.shadowRoot.getElementById("task-actions").hidden = cancel.hidden;
    this.shadowRoot.getElementById("instruction").disabled = this._busy || !canSend;
  }

  async _copyResponse() {
    const task = this._selectedTask();
    if (!task) return;
    const parts = [];
    if (String(task.summary || "").trim()) parts.push(`Summary\n${String(task.summary).trim()}`);
    const details = String(task.details || task.error || "").trim();
    if (details) parts.push(`Details\n${details}`);
    if (!parts.length) return;
    try {
      await this._writeClipboardText(parts.join("\n\n"));
      this._setMessage("Response copied.");
    } catch (error) {
      this._setMessage(`Could not copy the response: ${this._errorText(error)}`);
    }
  }

  async _writeClipboardText(text) {
    let clipboardError;
    if (navigator.clipboard?.writeText) {
      try {
        await navigator.clipboard.writeText(text);
        return;
      } catch (error) {
        clipboardError = error;
      }
    }

    const textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.readOnly = true;
    textarea.setAttribute("aria-hidden", "true");
    Object.assign(textarea.style, {
      position: "fixed",
      top: "0",
      left: "0",
      width: "1px",
      height: "1px",
      padding: "0",
      border: "0",
      opacity: "0",
    });
    document.body.appendChild(textarea);
    textarea.select();
    textarea.setSelectionRange(0, text.length);
    try {
      if (!document.execCommand("copy")) {
        throw clipboardError || new Error("Clipboard access is unavailable in this browser");
      }
    } finally {
      textarea.remove();
    }
  }

  _setMessage(message) {
    if (this._messageTimer) clearTimeout(this._messageTimer);
    this._messageTimer = null;
    this._message = message || "";
    const element = this.shadowRoot.getElementById("message");
    element.textContent = this._message;
    element.classList.toggle("show", Boolean(this._message));
    if (this._message) {
      this._messageTimer = setTimeout(() => {
        this._messageTimer = null;
        this._setMessage("");
      }, 6000);
    }
  }

  _errorText(error) {
    const candidates = [
      error?.error?.message,
      error?.body?.message,
      error?.response?.message,
      error?.message,
      typeof error?.error === "string" ? error.error : "",
    ];
    const message = candidates.find((value) => typeof value === "string" && value.trim());
    if (message) return message.trim();
    if (typeof error === "string" && error.trim()) return error.trim();
    return "Unknown Home Assistant service error";
  }
}

if (!customElements.get("codex-dashboard-card")) customElements.define("codex-dashboard-card", CodexDashboardCard);

window.customCards = window.customCards || [];
window.customCards.push({ type: "codex-dashboard-card", name: "Codex Dashboard", description: "Conversational Codex task dashboard" });
