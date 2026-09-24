# Codex CLI Worker

This app runs Codex CLI tasks against the Home Assistant config folder mounted at `/config`.

This app is distributed from:

```text
https://github.com/moryoav/home-assistant-codex
```

Prebuilt images are published to `ghcr.io/moryoav/codex-cli-worker` for `amd64` and `aarch64`.

## Security Model

The web UI is intended to be opened only through Home Assistant Ingress. The app does not publish its HTTP port to the LAN by default, and the Flask server rejects spoofed Ingress requests unless they come from the Home Assistant Ingress proxy address.

Programmatic API calls still require the internal worker API token unless they are proxied through authenticated Home Assistant Ingress. The Home Assistant Codex integration discovers the internal app hostname automatically through Supervisor metadata and provisions the worker token through Supervisor-managed app stdin.

The app does not request host networking, Docker API access, full access, host PID/UTS access, privileged kernel capabilities, or elevated Supervisor roles. It keeps AppArmor enabled and ships a custom `apparmor.txt` profile. The `/config` mount is intentionally read-write because editing Home Assistant configuration is the core purpose of the app.

For sandboxed modes, Codex uses Bubblewrap user, mount, PID, and network namespaces. Only the dedicated Bubblewrap setup binary enters a nested AppArmor profile that permits namespace setup. A wrapper forces Bubblewrap to drop all Linux capabilities before the generated command starts. On restrictive hosts that deny mounting a fresh `/proc`, Codex 0.146 and later automatically use their no-proc fallback while retaining the other namespace restrictions. That fallback can expose numeric process and file-descriptor counts from the app container, but it does not grant host PID access; sensitive process paths remain subject to kernel and AppArmor mediation.

## API Token

The API token protects the worker HTTP API. The app stores it in private app storage. The Home Assistant `Codex` integration provisions and rotates it automatically through Supervisor-managed app stdin. You do not need to view, copy, or configure this token.

The token is not your OpenAI or ChatGPT credential. Codex authentication is still handled separately with `codex login`.

## Model

`codex_model` is a fixed selection to avoid typo-prone free text. The default value, `default`, lets the installed Codex CLI choose its recommended model. Explicit choices are `gpt-6-astra` for the most demanding tasks, `gpt-5.6-sol` for complex work, `gpt-5.6-terra` for a balance of capability and cost, `gpt-5.6-luna` for fast and affordable work, and `gpt-5.5` as a previous-generation fallback. Model availability depends on your account. The app bundles Codex CLI 0.154.0, which includes Astra support. Older models are no longer offered in the selector. Existing installations with the legacy `gpt-5.3-codex` value remain upgrade-compatible and treat it as `default`.

`model_reasoning_effort` controls how much reasoning Codex asks supported models to use for each non-interactive task. The app passes it to `codex exec` as a per-run `--config model_reasoning_effort="<value>"` override rather than writing it into `config.toml`. Available values are:

- `minimal`: fastest and lowest reasoning.
- `low`: lighter reasoning.
- `medium`: balanced default.
- `high`: more reasoning, usually slower and more quota-intensive.
- `xhigh`: extra reasoning where the selected model supports it.

For GPT-6 Astra, select `low`, `medium`, `high`, or `xhigh`; `minimal` is not supported. See the [OpenAI model documentation](https://developers.openai.com/api/docs/models/gpt-6-astra) for supported reasoning levels and the [Codex models guide](https://learn.chatgpt.com/docs/models) for availability.

## Sandbox

- `read-only`: Codex can inspect files and run read-only commands, but should not edit `/config`.
- `workspace-write`: Codex can edit the mounted `/config` workspace while generated shell commands remain sandboxed. This is the recommended mode.
- `danger-full-access`: Codex sandboxing is disabled inside the add-on container. The container still only mounts the configured add-on volumes, but this should be used only when a task cannot work in `workspace-write`.

The worker exposes the Codex version and a structured sandbox preflight in its authenticated `/health` response. Sandboxed tasks fail with an actionable error before launch if namespace isolation is unavailable.

## Codex Sign-In

Use the add-on web UI or the Home Assistant service `codex_cli.start_login` to start `codex login --device-auth`.

Before starting sign-in, enable **Enable device code authorization for Codex** in ChatGPT: open the ChatGPT website, click your profile, open **Settings** -> **Security**, and turn on the toggle near the bottom of the page. This setting is not in the Codex website.

The add-on posts a Home Assistant persistent notification containing a QR code, link, and one-time device code. The QR code opens the OpenAI device page; type the code from the same notification into that page. The add-on refreshes the notification when the code appears and detects completion automatically. You do not need to run `docker exec`.

When opened through Home Assistant Ingress, the app web UI can start the login flow without entering the worker API token because Home Assistant already authenticated the session. Direct HTTP/API calls still require the worker API token.

Use **Settings → Sign out** in the app web UI or the Home Assistant service `codex_cli.logout` to run `codex logout` and remove saved Codex CLI credentials from the worker. Logout is blocked while a Codex task is actively running.

The app uses the built-in Supervisor token for Home Assistant notifications and dashboard saves through the Home Assistant Core API proxy. No Home Assistant long-lived access token is required.

Codex CLI sign-in uses your ChatGPT/OpenAI account. It may work with a free ChatGPT account, but ChatGPT Plus or higher is recommended for more reasonable usage limits. This project does not use OpenAI API keys for Codex tasks.

## AGENTS.md and HA_TOKEN

The app web UI includes an editor for `/config/AGENTS.md` under **Settings**. This file remains the source of truth for shared Codex project instructions.

The optional `HA_TOKEN` add-on option is passed to Codex subprocesses as the `HA_TOKEN` environment variable. Use a scoped Home Assistant token and only configure it if you want Codex tasks to call Home Assistant APIs directly.

## Usage Status

The worker performs a best-effort interactive probe of Codex CLI usage by starting a pseudo-terminal session and running `/status`. It extracts whichever usage windows Codex reports and exposes them through the worker `/status` payload, which the integration surfaces as sensors. Some accounts report both 5-hour and weekly windows, while others currently report only a weekly window.

The worker disables Codex update checks at startup. Codex CLI is installed and verified as part of the app image, so it must be updated by installing a newer app image rather than from an interactive Codex update prompt.

Because Codex does not currently provide a stable non-interactive usage command, a missing window has an `unknown` sensor state and a `reported: false` attribute. The existing 5-hour entities and worker fields remain in place so upgrades do not break automations on accounts that still report or reference them. Diagnostic excerpts remove account, email, and session identifiers before being exposed.

## Notifications

Leave `notify_service` unset or empty to use Home Assistant persistent notifications for task completion, failures, and questions. If you want push notifications, set it to a Home Assistant notify service such as `notify.mobile_app_your_phone`.

Home Assistant app configuration schemas do not currently provide a Home Assistant service autocomplete selector, so this option remains a plain text field.

## Human Input

Runs are non-interactive. If Codex needs a decision, it should return `needs_input`; Home Assistant marks the task as waiting and you can continue it with `codex_cli.reply_task`.

## Saved conversations

The web UI lists recent chats in a resizable sidebar and opens their messages on the right. On mobile, the sidebar becomes a drawer. Select a saved chat and send a message to continue, or choose **New chat** for a fresh session. Settings contains account controls and workspace instructions.

### Per-conversation model and reasoning

From **0.1.48**, the pill below the message box opens a model menu and a reasoning slider. Selecting a value in a saved chat saves it immediately; selections for a new chat are saved with its first message. Both persist across worker restarts. Changes apply to the next message and preserve the saved session. Controls are disabled while that conversation is running.

**Default** inherits the add-on model setting. The reasoning reset button inherits the add-on reasoning setting. These can be reset independently. New and older chats without saved overrides inherit both defaults. Each new turn records its resolved `execution_settings`, so changing the defaults after a turn is queued does not alter that run or its recorded settings.

The model choices match the add-on model selector. Supported reasoning levels follow the bundled CLI 0.154.0 catalog: Low, Medium, High, Extra High, Max and Ultra for Astra/Sol/Terra; through Max for Luna; through Extra High for GPT-5.5. Ultra enables automatic task delegation. The unspecified default model uses the common Low through Extra High choices. If switching models makes a saved reasoning choice incompatible, the UI resets reasoning to the add-on default. An inherited reasoning level unsupported by an explicit model falls back to Medium. Model availability still depends on the account; unavailable-model errors are reported by the CLI.

The authenticated worker API exposes `GET /chat-options` for choices and defaults, and `POST /tasks/<task_id>/settings` with `{"chat_settings": {"model": "gpt-6-astra", "reasoning_effort": "xhigh"}}` to save selections for an idle chat. Each value can be `null` to inherit the add-on default. `POST /tasks`, `/tasks/<task_id>/continue`, and `/tasks/<task_id>/reply` also accept the optional `chat_settings` object. Omitting the object preserves existing selections. Invalid models, reasoning levels, and combinations return HTTP 400; changes to active chats return HTTP 409. `GET /tasks/<task_id>` includes the saved `chat_settings`. Home Assistant actions continue using their existing parameters and honor settings already saved for the conversation.

### Generated images

From **0.1.49**, images that Codex generates during a conversation appear in the chat under the response that produced them. Click an image to open it at full size, or use **Download** to save it. Images stay with their exchange across reloads, worker restarts, and continued conversations.

Image generation uses the Codex CLI built-in `image_gen` tool and the signed-in ChatGPT account's Codex allowance. No OpenAI API key is required. Availability depends on the account, and the request counts toward the same quota as the rest of the task. This was verified with the bundled CLI 0.154.0 in non-interactive `codex exec` mode.

How the worker captures images: `codex exec --json` does not report image generation on standard output, so after each run the worker reads the session's rollout file under `/data/codex-home/sessions` and looks for completed `image_gen.generation` items recorded since the run started. Codex saves each result under `/data/codex-home/generated_images/<session_id>/`. The worker copies the file into `<task_root>/<task_id>/turns/<turn_id>/attachments/` and records metadata on that exchange. If the saved file is missing, the worker decodes the inline result from the rollout instead. The original stays where Codex saved it so follow-up edits in the same conversation keep working.

The task prompt tells Codex that generated images are attached automatically and that it should leave them at the default save location unless the user asks for a file at a specific path, for example under `/config/www` for use on a dashboard.

Limits and retention: only PNG, JPEG, GIF, and WebP content is accepted, checked by inspecting the file rather than trusting its name. Files larger than 25 MB and images beyond the first 12 per exchange are skipped and noted in the task log. Attachments are retained with the task history and are removed when the task directory is deleted; there is no automatic expiry. Generated images are private to the worker and are not written to `/config/www`.

API: `GET /tasks/<task_id>/attachments/<attachment_id>` returns the image with its recorded content type and `X-Content-Type-Options: nosniff`. Add `?download=1` for a download disposition. The endpoint requires the worker API token or an authenticated Home Assistant Ingress session. Before sending, it re-checks the stored file's size, SHA-256, and image signature against the recorded metadata, and returns HTTP 404 for unknown, missing, oversized, or altered files. `GET /tasks/<task_id>` and `codex_cli.get_task` include an `attachments` list on each turn and on the task-level result for the latest exchange. Each entry has `attachment_id`, `kind` (`image`), `name`, `mime_type`, `size`, `sha256`, `created_at`, `revised_prompt`, `generation_id`, `path` (relative to the task directory), and `url` (relative to the worker). The `codex_cli_task_result` event includes the same `attachments` list; it is empty for cancelled and failed launches.

If an image does not appear, open the task log and look for `Skipped image generation` or `Could not attach image generation` lines, which explain why the worker did not attach the file.

### Attaching images

From **0.1.51**, you can attach images to a message: a screenshot of a dashboard problem, a photo of a device, or a design mockup. Use the paperclip button next to the model pill, paste an image from the clipboard into the message box, or drop image files onto the composer. Pending images appear above the message box with a remove button and go out with the next message, in a new chat or when continuing a saved one. They then appear with your message in the conversation and stay with it across reloads and worker restarts.

Codex receives each image with the message through the CLI `--image` option (`codex exec --image` for a new chat, `codex exec resume --image` for a continued one; both verified with the bundled CLI 0.154.0), and the task prompt names the attached files. The images become part of the Codex session, so later turns in the same chat can still refer to them. They also count toward the context of every later turn in that chat, which is why the UI shrinks images whose longest edge exceeds 2048 pixels before uploading.

In the Home Assistant Android app, the paperclip picks one image at a time; use it again to add more. The app's file chooser returns nothing when a multi-select picker is used, so the web UI asks Android WebViews for a single selection. Paste and drag-and-drop still accept several images at once.

Limits: PNG, JPEG, GIF, and WebP only, checked by inspecting the bytes rather than the file name; at most 6 images per message and 10 MB per image after resizing. Attached images are stored under `<task_root>/<task_id>/turns/<turn_id>/attachments/`, are served only through the authenticated worker, are never written to `/config/www`, and are removed with the chat.

API: `POST /tasks`, `POST /tasks/<task_id>/continue`, and `POST /tasks/<task_id>/reply` accept an optional `attachments` list of `{"name": "shot.png", "data": "<base64>"}` objects; a `data:` URL prefix is tolerated. Invalid entries return HTTP 400 and no task or turn is created, and requests over roughly 81 MB return HTTP 413. Each turn in `GET /tasks/<task_id>` and `codex_cli.get_task` lists its uploads under `prompt_attachments` with the same fields as generated images plus `origin: "user"`; generated images now carry `origin: "codex"`. `GET /tasks/<task_id>/attachments/<attachment_id>` serves both kinds with the same checks. The `attachments` list on the task result and in the `codex_cli_task_result` event still contains only images generated by Codex.

### Managing chats

From **0.1.50**, each chat in the sidebar has an actions menu. On desktop, hover over a chat and choose the **⋯** button, or right-click the row. On phones, long-press the chat to open a bottom sheet. With a keyboard, focus a chat and press Shift+F10; arrow keys move between items and Escape closes the menu.

**Pin chat** keeps the conversation at the top of the list under a **Pinned** heading, ahead of the recently active chats; **Unpin chat** returns it to the recent list. **Rename** opens a dialog for a new title, which appears in the sidebar and the conversation header. Neither action changes the chat's `updated_at` time or its saved messages. **Delete** asks for confirmation, then removes the conversation and its saved history. The delete action is unavailable while that chat is running; stop the task first.

Deleting a chat removes `<task_root>/<task_id>/`, including prompts, output, snapshots, and attachments, and the entry from the task index. The worker also removes the Codex session rollout file under `/data/codex-home/sessions` and any generated images under `/data/codex-home/generated_images/<session_id>/` so the conversation cannot be resumed or recovered from the add-on data. This cannot be undone.

API: `POST /tasks/<task_id>/pin` with `{"pinned": true}` or `{"pinned": false}` returns the saved state. `POST /tasks/<task_id>/title` with `{"title": "Porch lights"}` trims and collapses whitespace, rejects empty titles and titles longer than 200 characters with HTTP 400, and returns the stored title. Both work on running chats and return HTTP 404 for unknown tasks and HTTP 500 if the metadata could not be written, in which case the previous values are kept. `DELETE /tasks/<task_id>` returns HTTP 409 for queued or running tasks, HTTP 404 for unknown tasks, and HTTP 500 if the task files could not be removed, in which case the chat stays listed. Summary entries from `GET /tasks?summary=true` include a `pinned` flag, and `order=pinned_first` lists pinned chats before the others while keeping the most recently updated first within each group. The `codex_cli.list_tasks` action keeps its existing `created_asc` and `updated_desc` orders.

### Continuing and browsing chats

`codex_cli.continue_task` accepts `task_id` and `message` and resumes completed, waiting, failed, or cancelled tasks. The old `reply_task` action remains restricted to tasks waiting for input. Both use the same resume implementation. Continuation requires a saved session under `/data/codex-home/sessions`; the worker does not fall back to a fresh chat if it is missing.

`GET /tasks` and `codex_cli.list_tasks` accept optional `limit` (1–500), `offset`, `status`, `order` (`created_asc` or `updated_desc`; the worker API also accepts `pinned_first`), and `summary` parameters. `summary: true` returns compact entries for the sidebar. Without filters, all tasks are returned in their original creation order. `GET /tasks/<task_id>` includes `turns`, `history_incomplete`, and `can_continue`. The latter indicates an eligible task with a recorded session ID; availability of its session file is checked when continuing. `POST /tasks/<task_id>/continue` accepts a JSON `message`.

Each new exchange stores its user message, timestamps, status, and response in the task's `turns` list. Prompts, final output, snapshots, and change manifests are stored separately under `<task_root>/<task_id>/turns/<turn_id>/`. The task-level result and existing result events continue to describe the latest exchange. Old tasks are adapted from their available prompt, replies, and last result, with a notice that earlier responses may be missing. History is retained until its files are removed; there is no automatic expiry.

For local browser checks, run `python codex-cli-worker/tests/web_fixture.py` from the repository root, then `node codex-cli-worker/tests/browser_smoke.cjs` with Playwright available. The fixture uses temporary storage and simulated responses, and never launches Codex or contacts Home Assistant. Set `CODEX_CHAT_BROWSER=msedge` to use installed Edge, and optionally `CODEX_CHAT_QA_DIR` to choose a screenshot directory outside the repository.
