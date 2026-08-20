class CodexPromptCard extends HTMLElement {
  static getStubConfig() {
    return {};
  }

  constructor() {
    super();
    this.attachShadow({ mode: "open" });
    this._hass = null;
    this._config = {};
    this._activeTaskId = null;
    this._taskStatus = null;
    this._messages = [];
    this._uiStatus = "Ready";
    this._pollTimer = null;
    this._pollFailures = 0;
    this._stopping = false;
    this._storageKey = "codex-prompt-card-state-v1";
  }

  setConfig(config) {
    this._config = config || {};
    if (!this.shadowRoot.firstChild) {
      this._render();
      this._restore();
    }
  }

  set hass(hass) {
    this._hass = hass;
    if (this._activeTaskId && !this._pollTimer && this._isActive(this._taskStatus)) {
      this._schedulePoll(0);
    }
  }

  getCardSize() {
    return 6;
  }

  disconnectedCallback() {
    this._cancelPoll();
  }

  _render() {
    this.shadowRoot.innerHTML = `
      <style>
        :host { display: block; }
        ha-card {
          color: var(--primary-text-color);
          background: var(--ha-card-background, var(--card-background-color));
          overflow: hidden;
        }
        .header {
          display: flex;
          align-items: center;
          justify-content: space-between;
          gap: 12px;
          padding: 16px 16px 12px;
          border-bottom: 1px solid var(--divider-color);
        }
        .title { font-size: 1.2rem; font-weight: 500; }
        .status { display: flex; align-items: center; gap: 7px; color: var(--secondary-text-color); font-size: .9rem; }
        .dot { width: 9px; height: 9px; border-radius: 50%; background: var(--secondary-text-color); }
        .status.working .dot, .status.stopping .dot { background: var(--primary-color); animation: pulse 1.2s infinite; }
        .status.waiting .dot { background: var(--warning-color, #ff9800); }
        .status.error .dot { background: var(--error-color); }
        @keyframes pulse { 50% { opacity: .35; } }
        .conversation {
          box-sizing: border-box;
          height: min(430px, 52vh);
          min-height: 260px;
          overflow-y: auto;
          padding: 16px;
          scroll-behavior: smooth;
          user-select: text;
          -webkit-user-select: text;
        }
        .empty { color: var(--secondary-text-color); text-align: center; margin: 80px 16px; }
        .message { display: flex; flex-direction: column; margin: 0 0 14px; }
        .message.user { align-items: flex-end; }
        .message.codex { align-items: flex-start; }
        .label { color: var(--secondary-text-color); font-size: .75rem; margin: 0 7px 4px; }
        .bubble {
          box-sizing: border-box;
          max-width: 88%;
          padding: 10px 12px;
          border-radius: 14px;
          white-space: pre-wrap;
          overflow-wrap: anywhere;
          line-height: 1.45;
          background: var(--secondary-background-color);
          border: 1px solid var(--divider-color);
          cursor: text;
        }
        .user .bubble { color: var(--text-primary-color, #fff); background: var(--primary-color); border-color: transparent; }
        .question .bubble { border-color: var(--warning-color, #ff9800); }
        .error-message .bubble { border-color: var(--error-color); }
        .composer { padding: 12px 16px 16px; border-top: 1px solid var(--divider-color); }
        textarea {
          box-sizing: border-box;
          width: 100%;
          min-height: 82px;
          max-height: 220px;
          resize: vertical;
          padding: 10px 12px;
          color: var(--primary-text-color);
          background: var(--input-fill-color, transparent);
          border: 1px solid var(--input-idle-line-color, var(--divider-color));
          border-radius: 8px;
          font: inherit;
          line-height: 1.4;
        }
        textarea:focus { outline: 2px solid var(--primary-color); outline-offset: -1px; }
        .actions { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 10px; }
        .right-actions { display: flex; gap: 8px; }
        button {
          min-height: 38px;
          padding: 0 14px;
          border: 0;
          border-radius: 8px;
          font: inherit;
          font-weight: 500;
          cursor: pointer;
          color: var(--primary-text-color);
          background: var(--secondary-background-color);
        }
        button.primary { color: var(--text-primary-color, #fff); background: var(--primary-color); }
        button.stop { color: var(--error-color); }
        button:disabled { opacity: .45; cursor: default; }
        .hint { color: var(--secondary-text-color); font-size: .75rem; margin-top: 7px; }
        @media (max-width: 500px) {
          .conversation { height: 46vh; min-height: 240px; padding: 12px; }
          .bubble { max-width: 95%; }
          .composer { padding: 10px 12px 12px; }
          .actions { align-items: stretch; }
          button { padding: 0 11px; }
        }
      </style>
      <ha-card>
        <div class="header">
          <div class="title">${this._escape(this._config.title || "Codex Chat")}</div>
          <div id="status" class="status ready"><span class="dot"></span><span>Ready</span></div>
        </div>
        <div id="conversation" class="conversation"><div class="empty">Start a task to chat with Codex.</div></div>
        <div class="composer">
          <textarea id="prompt" placeholder="Ask Codex to work on Home Assistant…" aria-label="Message to Codex"></textarea>
          <div class="actions">
            <button id="clear" type="button">Clear</button>
            <div class="right-actions">
              <button id="stop" class="stop" type="button" disabled>Stop</button>
              <button id="send" class="primary" type="button">Send</button>
            </div>
          </div>
          <div class="hint">Press Ctrl+Enter or Cmd+Enter to send.</div>
        </div>
      </ha-card>`;

    this.$("send").addEventListener("click", () => this._send());
    this.$("stop").addEventListener("click", () => this._stop());
    this.$("clear").addEventListener("click", () => this._clear());
    this.$("prompt").addEventListener("keydown", (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        this._send();
      }
    });
  }

  $(id) {
    return this.shadowRoot.getElementById(id);
  }

  async _send() {
    const input = this.$("prompt");
    const text = input.value.trim();
    if (!text || !this._hass || this._stopping) return;

    input.value = "";
    this._messages.push({ role: "user", text });
    this._updateConversation();
    this._setControls(true);
    this._setStatus("Working");

    try {
      if (this._activeTaskId && this._isWaiting(this._taskStatus)) {
        await this._callService("reply_task", { task_id: this._activeTaskId, reply: text });
      } else {
        const response = await this._callService("start_task", { prompt: text });
        this._activeTaskId = response && response.task_id;
        if (!this._activeTaskId) throw new Error("The service did not return a task ID.");
      }
      this._taskStatus = "queued";
      this._pollFailures = 0;
      this._save();
      this._schedulePoll(350);
    } catch (error) {
      this._addError(this._errorText(error));
      this._setStatus("Error");
      this._setControls(false);
      this._save();
    }
  }

  async _poll() {
    this._pollTimer = null;
    if (!this._activeTaskId || !this._hass) return;

    try {
      const response = await this._callService("get_task", { task_id: this._activeTaskId });
      const task = response && response.task;
      if (!task) throw new Error("The service did not return task details.");
      this._pollFailures = 0;
      this._taskStatus = task.status;
      this._applyTask(task);
      this._save();

      if (this._isActive(task.status)) {
        this._schedulePoll(2000);
      }
    } catch (error) {
      this._pollFailures += 1;
      if (this._pollFailures >= 3) this._setStatus("Error");
      const delay = Math.min(15000, 1500 * (2 ** Math.min(this._pollFailures - 1, 3)));
      this._schedulePoll(delay);
    }
  }

  _applyTask(task) {
    const status = task.status || "failed";
    if (task.task_id) this._activeTaskId = task.task_id;

    if (this._isActive(status)) {
      this._setStatus(this._stopping ? "Stopping" : "Working");
      this._setControls(true);
      return;
    }

    this._stopping = false;
    if (this._isWaiting(status)) {
      this._upsertTaskMessage("question", task.question || task.summary || "Codex needs your input.", "question");
      this._setStatus("Waiting");
      this._setControls(false, true);
      return;
    }

    if (status === "completed") {
      this._upsertTaskMessage("result", task.details || task.summary || "Task completed.");
      this._setStatus("Ready");
    } else if (status === "cancelled" || status === "canceled") {
      this._upsertTaskMessage("result", task.details || task.summary || "Task stopped.");
      this._setStatus("Ready");
    } else if (status === "failed" || status === "error") {
      const text = task.details || task.error || task.summary || "The task failed.";
      this._upsertTaskMessage("result", text, "error");
      this._setStatus("Error");
    } else {
      this._upsertTaskMessage("result", `Unknown task status: ${status}`, "error");
      this._setStatus("Error");
    }
    this._setControls(false);
  }

  async _stop() {
    if (!this._activeTaskId || !this._isActive(this._taskStatus) || !this._hass) return;
    this._stopping = true;
    this._setStatus("Stopping");
    this._setControls(true);
    try {
      const response = await this._callService("cancel_task", { task_id: this._activeTaskId });
      this._taskStatus = (response && response.status) || "cancelled";
      this._schedulePoll(0);
    } catch (error) {
      this._stopping = false;
      this._addError(this._errorText(error));
      this._setStatus("Error");
      this._setControls(false);
    }
  }

  _clear() {
    this._cancelPoll();
    this._messages = [];
    this._activeTaskId = null;
    this._taskStatus = null;
    this._stopping = false;
    this._pollFailures = 0;
    try { localStorage.removeItem(this._storageKey); } catch (_error) { /* Storage is optional. */ }
    this._updateConversation();
    this._setStatus("Ready");
    this._setControls(false);
    this.$("prompt").focus();
  }

  _callService(service, data) {
    return this._hass.callService("codex_cli", service, data, {}, true);
  }

  _upsertTaskMessage(kind, text, variant = "") {
    const key = `${this._activeTaskId}:${kind}`;
    const existing = this._messages.find((message) => message.key === key);
    if (existing) {
      existing.text = String(text);
      existing.variant = variant;
    } else {
      this._messages.push({ role: "codex", text: String(text), key, variant });
    }
    this._updateConversation();
  }

  _addError(text) {
    this._messages.push({ role: "codex", text, variant: "error" });
    this._updateConversation();
  }

  _updateConversation() {
    const container = this.$("conversation");
    if (!container) return;
    if (!this._messages.length) {
      container.innerHTML = '<div class="empty">Start a task to chat with Codex.</div>';
    } else {
      container.innerHTML = this._messages.map((message) => {
        const role = message.role === "user" ? "user" : "codex";
        const variant = message.variant === "question" ? " question" : message.variant === "error" ? " error-message" : "";
        const label = role === "user" ? "You" : "Codex";
        return `<div class="message ${role}${variant}"><div class="label">${label}</div><div class="bubble">${this._escape(message.text)}</div></div>`;
      }).join("");
    }
    requestAnimationFrame(() => { container.scrollTop = container.scrollHeight; });
  }

  _setStatus(status) {
    this._uiStatus = status;
    const element = this.$("status");
    if (!element) return;
    element.className = `status ${status.toLowerCase()}`;
    element.lastElementChild.textContent = status;
  }

  _setControls(active, waiting = false) {
    const isActive = active || this._isActive(this._taskStatus);
    this.$("send").disabled = isActive && !waiting;
    this.$("stop").disabled = !isActive || this._stopping;
    this.$("prompt").placeholder = waiting ? "Reply to Codex…" : "Ask Codex to work on Home Assistant…";
  }

  _isActive(status) {
    return status === "queued" || status === "starting" || status === "running";
  }

  _isWaiting(status) {
    return status === "waiting_for_input" || status === "needs_input" || status === "waiting" || status === "question";
  }

  _schedulePoll(delay) {
    this._cancelPoll();
    this._pollTimer = setTimeout(() => this._poll(), delay);
  }

  _cancelPoll() {
    if (this._pollTimer) clearTimeout(this._pollTimer);
    this._pollTimer = null;
  }

  _save() {
    try {
      localStorage.setItem(this._storageKey, JSON.stringify({
        taskId: this._activeTaskId,
        taskStatus: this._taskStatus,
        messages: this._messages,
      }));
    } catch (_error) { /* The card still works if storage is unavailable. */ }
  }

  _restore() {
    try {
      const saved = JSON.parse(localStorage.getItem(this._storageKey) || "null");
      if (!saved || !saved.taskId) return;
      this._activeTaskId = saved.taskId;
      this._taskStatus = saved.taskStatus || "queued";
      this._messages = Array.isArray(saved.messages) ? saved.messages : [];
      this._updateConversation();
      this._setStatus(this._isWaiting(this._taskStatus) ? "Waiting" : this._isActive(this._taskStatus) ? "Working" : "Ready");
      this._setControls(this._isActive(this._taskStatus), this._isWaiting(this._taskStatus));
      this._schedulePoll(0);
    } catch (_error) {
      try { localStorage.removeItem(this._storageKey); } catch (_storageError) { /* Ignore. */ }
    }
  }

  _errorText(error) {
    return (error && (error.message || (error.body && error.body.message))) || "The service call failed.";
  }

  _escape(value) {
    return String(value == null ? "" : value)
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }
}

if (!customElements.get("codex-prompt-card")) {
  customElements.define("codex-prompt-card", CodexPromptCard);
}

window.customCards = window.customCards || [];
window.customCards.push({
  type: "codex-prompt-card",
  name: "Codex Prompt Card",
  description: "Start and follow Codex CLI Worker tasks from Lovelace.",
});
