# Codex CLI Worker

This app runs Codex CLI tasks against the Home Assistant config folder mounted at `/config`.

## Dashboard browser verification

**Codex can open your dashboards in a real browser and inspect desktop and mobile
screenshots while working on a change.** It can review the layout and revise its
work. Screenshots and findings appear with the conversation, including missing
cards, failed resources, and browser errors.

For example, ask: **"Inspect `/lovelace/home` on desktop and mobile, show me
screenshots, and report any layout or card errors."** Replace the path with your
dashboard view. Saved storage dashboards also get automatic captures of the first
views that fit the task's remaining time. Request other views and YAML dashboards
explicitly. Automatic captures after the answer are labeled separately from
screenshots Codex inspected during the task.

Browser sign-in uses a temporary, local, read-only identity, without a Home
Assistant password or manually created token. Tasks can also fetch fresh entity
states, read Core logs, and check configuration. See [verification tools,
authentication, screenshots, and limits](VERIFICATION.md).

From 0.1.61, dashboard captures also load integrations' registered static assets
and supported public HTTPS scripts, styles, and fonts. Update **both** the app and
integration to **0.1.62** and restart Home Assistant for static-route discovery,
including individual static files on Core 2026.9.4.
Repeated findings are grouped by cause and viewport. Service calls and writes
remain blocked. See [resource compatibility and privacy](VERIFICATION.md#dashboard-resources).

**Enable built-in browser** in the app's **Configuration** tab controls dashboard
inspection and screenshots. The browser uses RAM while it runs. Turn this off
to completely disable browser launches if you do not need them or have limited
RAM; state and configuration checks remain available.

From 0.1.60, **Browser memory limit (MiB)** in the same tab
controls the memory budget for dashboard captures. The default is **1536 MiB
(1.5 GiB)**, with a range of **512 to 8192 MiB**. For larger dashboards, increase
it to values such as 2048 or 3072 if the host has enough free memory alongside
Home Assistant and other apps. This is a capture limit, not reserved RAM or a
limit on the whole app. Save the options and restart the app if Home Assistant
prompts you; the next capture uses the new value.

## Distribution

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

`codex_model` is a fixed selection to avoid typo-prone free text. The default value, `default`, lets the installed Codex CLI choose its recommended model. Explicit choices are `gpt-6-astra` for the most demanding tasks, `gpt-6.1-sol` as the latest workhorse for coding and everyday work, close to Astra at a lower cost, `gpt-6-sol` as the previous workhorse, `gpt-6-luna` for fast and affordable work on easier tasks, and the older `gpt-5.6-sol`, `gpt-5.6-terra`, and `gpt-5.6-luna`. Model availability depends on your account. The app bundles Codex CLI 0.160.0, which includes GPT-6.1 Sol support. Older models are no longer offered in the selector. Existing installations with the legacy `gpt-5.3-codex` value remain upgrade-compatible and treat it as `default`. `gpt-5.5` is retired: OpenAI removes it from Codex with ChatGPT sign-in on October 14, 2026. It stays in the list only so that an app with it saved keeps starting, and it runs `gpt-5.6-sol`, the next model up.

`model_reasoning_effort` controls how much reasoning Codex asks supported models to use for each non-interactive task. The app passes it to `codex exec` as a per-run `--config model_reasoning_effort="<value>"` override rather than writing it into `config.toml`. Available values are:

- `minimal`: fastest and lowest reasoning.
- `low`: lighter reasoning.
- `medium`: balanced default.
- `high`: more reasoning, usually slower and more quota-intensive.
- `xhigh`: extra reasoning where the selected model supports it.

For GPT-6 Astra, GPT-6.1 Sol, GPT-6 Sol, and GPT-6 Luna, select `low`, `medium`, `high`, or `xhigh`; `minimal` is not supported. None of the current models supports `minimal`, so the app runs `medium` when it is selected, also with the `default` model. See the [OpenAI model documentation](https://developers.openai.com/api/docs/models/gpt-6-astra) for supported reasoning levels and the [Codex models guide](https://learn.chatgpt.com/docs/models) for availability.

`reasoning_summary` controls whether Codex reports its reasoning while it works, which the chat shows in the activity list under your latest message. It is passed to `codex exec` as `--config model_reasoning_summary="<value>"`. Codex reports no reasoning at all unless summaries are requested, so the default is on.

- `concise`: short headlines such as "Checking the automation". This is the default.
- `detailed`: asks for fuller summaries; current models still mostly return headlines.
- `none`: no reasoning in the activity list. Commands, file edits, searches, progress notes, and tool calls are still shown.

There is no web UI control for this setting; change it in the add-on configuration.

## Validation

After every task the worker compares the `/config` tree with the list of files it recorded before the run, then validates what changed in two passes.

1. **Syntax.** Changed YAML files are parsed with Home Assistant's YAML loader and changed JSON and `.storage` files are parsed as JSON. Any parse error fails the task.
2. **Home Assistant's configuration check.** From **0.1.56**, when YAML files outside `.storage` were added, changed, or deleted and the syntax pass succeeded, the worker calls `POST /api/config/core/check_config`, the same check as Developer Tools. An `invalid` result fails the task with Home Assistant's error text. The check needs the Supervisor's Core API access the app already has, and can take some seconds on large configurations; the worker waits up to three minutes. If the check cannot run, because Core is restarting or the API is unreachable, the task still completes and its details say the change is applied but unverified. Set `config_check` to `false` to skip this pass.

When validation fails, the task details list the saved previous version of each affected file under `<task_root>/<task_id>/turns/<turn_id>/backups/`, so a broken edit can be restored by copying the file back. Files the task created have no previous version and are listed separately, and so are affected files that have no saved copy. See [Backups](#backups) for where the copies come from and how long they are kept.

Dashboard auto-save skips storage files that failed validation and reports the skip in `lovelace_results`. A YAML failure does not block saving an unrelated, valid dashboard file.

The chat shows the outcome under the answer: a passing check as a short confirmation, a failing check with Home Assistant's error, and an unavailable check as a note. `GET /tasks/<task_id>`, `codex_cli.get_task`, and the `codex_cli_task_result` event carry `config_check` with `result` (`valid`, `invalid`, `unavailable`, `skipped` when no YAML changed or syntax already failed, or `disabled`), `errors`, and `warnings`, and `recovery_files` with `path` and `copy` per affected file. An entry without a copy has a `reason` of `no_backup` or `excluded_credentials`, unless the task created the file. A configuration check does not prove that an automation behaves as intended; it only confirms Home Assistant would load the configuration.

## Backups

From **0.1.63**, the worker no longer archives the whole configuration folder before every message. On a large configuration that archive took longer than Codex needed to start answering, and one archive was kept per message forever.

**Per-file copies (default).** The task prompt tells Codex to copy each existing file to `<task_root>/<task_id>/turns/<turn_id>/backups/<path under /config>` before it changes, moves, or deletes it. Only the files Codex is about to change are copied. When the run ends the worker reviews those copies:

- A copy counts only when it is identical to the file as the worker saw it before the run started. A copy made after the first edit is reported as not saved.
- The review covers the files that are Codex's doing: files it reported editing, files it copied, changed YAML files, and changed dashboard storage files. Home Assistant rewrites its own `.storage` files all the time, so other changed files are not reviewed.
- The chat lists the files with a saved previous version, and the task details name any reviewed file without one. `GET /tasks/<task_id>`, `codex_cli.get_task`, and the `codex_cli_task_result` event carry `backups` with `path`, `status` (`saved`, `missing`, `unverified` for a copy that is not the earlier version, or `excluded`), and `copy`.
- Credential files (`secrets.yaml`, the auth stores, and `.storage/core.config_entries`) are never copied. The worker deletes such a copy if Codex makes one.

Codex needs to be able to write to the backup folder. In `workspace-write` mode that requires `task_root` to be inside `/config`, which is the default. In `read-only` mode Codex changes nothing and is given no backup folder. A per-file copy depends on Codex following the instruction; if a file was changed without one, restore it from a Home Assistant backup.

**Full snapshot (optional).** Turn on **Full snapshot before every message** (`full_snapshot`) to archive every tracked file to `snapshot-before.tar.gz` in the exchange's folder before Codex starts, as earlier versions did. The worker then takes the previous version of any reviewed file that Codex did not copy out of the archive, so every reviewed file has a saved copy. The archive adds a wait before every message that grows with the size of `/config`.

**Cleanup.** Backups are deleted **Keep backups for (days)** (`backup_retention_days`, default 7, range 1 to 365) after their exchange ended. This covers per-file copies, full snapshots, and recovery copies made by earlier versions. The cleanup runs when the app starts, after every exchange, and once an hour while the app is idle; it skips the exchange that is running and never touches messages, logs, or attachments. Archives left by earlier versions are removed the same way once they are older than the setting. The chat notes when the saved copies of an exchange have been removed.

**Change detection.** The list of files the worker records before a run holds each file's size, modification time, and SHA-256 hash. A file whose size and timestamps are unchanged since the previous scan is not read again, so only the first scan after the app starts reads every file. The list is deleted when the exchange ends; the files that changed stay recorded in `changes.json`. The worker's own task folder is left out of the list.

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

From **0.1.68**, a task that has `HA_TOKEN` also gets `HA_URL`, the address those calls go to, and the worker's instructions tell Codex to use the two together. The address is the **Home Assistant API URL** option (`ha_url`), which now defaults to `http://homeassistant:8123`, Home Assistant's own address inside the app network. Change it only if your Home Assistant uses HTTPS or another port. Before, Codex was given the token without an address and tended to use `http://supervisor/core`, the Supervisor's Core proxy, which rejects a Home Assistant token with HTTP 401; a reload or restart it was asked for then failed. An installation that still has `http://supervisor/core` saved in the option gets the new default, so nothing has to be changed after the update. The app's own calls to Home Assistant keep using the Supervisor proxy with the app's own token and do not use this option.

These calls come from Codex's shell, so they need a sandbox mode with network access; `workspace-write` and `read-only` block it. What Codex can do with the token is whatever the token's user may do, including reloading configuration and restarting Home Assistant.

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

Below **Home Assistant workspace**, the sidebar shows how much of the account's 5-hour and 7-day quota is left. From **0.1.67**, each has a bar next to the percentage. The filled part is the quota left: green, and red when less than 5% is left. Hover over a bar to see when the quota resets. The worker checks the quota every few minutes, and not while a task is running. Until it has a value, the bar is an empty outline.

From **0.1.54**, the web UI reopens the chat you left open the next time it loads, for example after the Home Assistant app on a phone reloads the panel. Choosing **New chat** is remembered as well. The chat id is kept in the browser's local storage under the Home Assistant origin, so it applies to that browser only. Opening the chat requests it from the worker as usual, but the worker does not record which chat you had open. If the remembered chat was deleted from another tab or device, the UI starts with a new chat instead.

### Formatted messages

From **0.1.66**, the chat formats Markdown in the messages you send and in Codex's answers. Wrap code in triple backticks to get a code block, with an optional language name after the opening backticks:

````text
Why does this trigger never fire?

```yaml
trigger:
  - platform: sun
    event: sunset
```
````

Single backticks mark inline code, such as an entity id. Headings, bold and italic text, strikethrough, bulleted and numbered lists, task lists, quotes, tables, horizontal rules, and links are formatted as well. Code blocks keep their indentation and scroll sideways when a line is too long, and so do wide tables. A single Enter stays a line break, and `<br>` breaks a line inside a table cell. The preview of each chat in the sidebar shows the answer without the symbols.

Your own messages follow the same rules, so a line that starts with `#`, `-`, or `1.` becomes a heading or a list item, and text between asterisks or underscores is emphasized. Put configuration, templates, and logs in a code block to show them exactly as written.

Only the display changes. Codex receives your message exactly as you typed it, the saved conversation keeps the original text, and Home Assistant actions and the `codex_cli_task_result` event return `summary`, `question`, and `details` unchanged. The steps in the activity list are not formatted.

Messages are treated as untrusted text:

- HTML is shown as typed and is never interpreted. The one exception is `<br>` inside a table cell, which breaks the line.
- Links open in a new tab and are limited to `http`, `https`, and `mailto` addresses. Any other target, such as a file path or a script address, shows its label as plain text.
- Images are never loaded from a message, because a remote image would tell its server that the message was read. They appear as links. Images that Codex generates or that you attach are shown as before.
- A message longer than 50,000 characters, or one that takes unusually long to format, is shown as plain text.

The Markdown parser, [marked](https://github.com/markedjs/marked) 18.0.14, is bundled with the app under `web/assets/vendor/` with its MIT license, so the chat does not load scripts from another server. Codex decides how it writes its answers; to ask for Markdown, add an instruction to `/config/AGENTS.md` as described under [Task Output](https://github.com/moryoav/home-assistant-codex/blob/main/README.md#task-output).

### Per-conversation model and reasoning

From **0.1.48**, the pill below the message box opens a model menu and a reasoning slider. Selecting a value in a saved chat saves it immediately; selections for a new chat are saved with its first message. Both persist across worker restarts. Changes apply to the next message and preserve the saved session. Controls are disabled while that conversation is running.

A chat without a model of its own follows the add-on model setting. From **0.1.67**, the pill names that model instead of showing **Default**: the model set in the add-on options or, when that option is `default`, the model Codex picks for the signed-in account. The worker reads the latter from Codex's own status report when it checks the quota, and shows the model the bundled CLI recommends, GPT-6.1 Sol, until the first report. The menu marks that model, and choosing it makes the chat follow the add-on setting again. The reasoning reset button inherits the add-on reasoning setting. These can be reset independently. New and older chats without saved overrides inherit both defaults. Each new turn records its resolved `execution_settings`, so changing the defaults after a turn is queued does not alter that run or its recorded settings.

The model choices match the add-on model selector, without the retired `gpt-5.5`. Supported reasoning levels follow the bundled CLI 0.160.0 catalog: Low, Medium, High, Extra High, Max and Ultra for GPT-6 Astra, GPT-6.1 Sol, GPT-6 Sol, GPT-5.6 Sol and GPT-5.6 Terra; through Max for GPT-6 Luna and GPT-5.6 Luna. Ultra enables automatic task delegation. With the add-on model on `default`, the levels are those of the model Codex picks; a model the worker has no entry for gets the common Low through Extra High choices. If switching models makes a saved reasoning choice incompatible, the UI resets reasoning to the add-on default. An inherited reasoning level that the model does not support falls back to Medium. So does a level saved in a chat that follows the add-on model, when that model changes to one without the level; the saved level applies again once the model has it. A chat that had GPT-5.5 selected continues on GPT-5.6 Sol, the next model up, and keeps its reasoning level. Model availability still depends on the account; unavailable-model errors are reported by the CLI.

The authenticated worker API exposes `GET /chat-options` for choices and defaults, with `default_model` naming the model a chat without its own selection runs on, and `POST /tasks/<task_id>/settings` with `{"chat_settings": {"model": "gpt-6-astra", "reasoning_effort": "xhigh"}}` to save selections for an idle chat. Each value can be `null` to inherit the add-on default. `POST /tasks`, `/tasks/<task_id>/continue`, and `/tasks/<task_id>/reply` also accept the optional `chat_settings` object. Omitting the object preserves existing selections. Invalid models and reasoning levels, and a level the selected model lacks, return HTTP 400; changes to active chats return HTTP 409. `GET /tasks/<task_id>` includes the saved `chat_settings`. Home Assistant actions continue using their existing parameters and honor settings already saved for the conversation.

### Live activity

From **0.1.53**, the chat shows what Codex is doing while it works on your latest message. Steps appear under the message as Codex reports them: reasoning headlines, progress notes, the commands it runs, the files it edits, web searches, and tool calls. A failed command or an error is highlighted. When a run is stopped or fails, the list ends with a **Stopped** or **Failed** row. The list is expanded while the run lasts and collapses to **Show activity (N steps)** under the answer when it finishes. Only the latest exchange of a chat shows steps; earlier exchanges do not.

From **0.1.63**, the list starts as soon as a message is accepted. The worker reports its own steps alongside those of Codex: recording the state of the configuration files, saving a full snapshot when that option is on, starting Codex, waiting for its first response, and checking the changes afterwards. The heading shows how long the exchange has been running, and its dot pulses for as long as the run lasts.

Command output is not shown by default because it can contain configuration secrets. Each command offers **Show output**, which reveals the first 2 KB after the same redaction as the task log and marks output that was cut. File edits list paths only. Long reasoning or messages are cut at 4 KB, and at most 500 steps are kept per exchange; the full record remains in the task's `codex.log`.

The worker keeps the steps of the running exchange in memory and writes them to `<task_root>/<task_id>/turns/<turn_id>/activity.json` once when the run ends, so the latest exchange's steps survive a page reload and a worker restart. The authenticated worker API exposes `GET /tasks/<task_id>/activity?after=<seq>`, which returns `turn_id`, `running`, `seq`, `total`, the `steps` whose sequence number is above `after`, and `elapsed_ms` while the exchange runs. A step that changes, such as a command that finishes, is returned again with a new sequence number and the same `index`. The web UI polls this endpoint once a second while a chat is working and pauses when the tab is hidden. The Home Assistant integration does not use it.

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

Deleting a chat removes `<task_root>/<task_id>/`, including prompts, output, backups, and attachments, and the entry from the task index. The worker also removes the Codex session rollout file under `/data/codex-home/sessions` and any generated images under `/data/codex-home/generated_images/<session_id>/` so the conversation cannot be resumed or recovered from the add-on data. This cannot be undone.

API: `POST /tasks/<task_id>/pin` with `{"pinned": true}` or `{"pinned": false}` returns the saved state. `POST /tasks/<task_id>/title` with `{"title": "Porch lights"}` trims and collapses whitespace, rejects empty titles and titles longer than 200 characters with HTTP 400, and returns the stored title. Both work on running chats and return HTTP 404 for unknown tasks and HTTP 500 if the metadata could not be written, in which case the previous values are kept. `DELETE /tasks/<task_id>` returns HTTP 409 for queued or running tasks, HTTP 404 for unknown tasks, and HTTP 500 if the task files could not be removed, in which case the chat stays listed. Summary entries from `GET /tasks?summary=true` include a `pinned` flag, and `order=pinned_first` lists pinned chats before the others while keeping the most recently updated first within each group. The `codex_cli.list_tasks` action keeps its existing `created_asc` and `updated_desc` orders.

### Continuing and browsing chats

`codex_cli.continue_task` accepts `task_id` and `message` and resumes completed, waiting, failed, or cancelled tasks. The old `reply_task` action remains restricted to tasks waiting for input. Both use the same resume implementation. Continuation requires a saved session under `/data/codex-home/sessions`; the worker does not fall back to a fresh chat if it is missing.

`GET /tasks` and `codex_cli.list_tasks` accept optional `limit` (1–500), `offset`, `status`, `order` (`created_asc` or `updated_desc`; the worker API also accepts `pinned_first`), and `summary` parameters. `summary: true` returns compact entries for the sidebar. Without filters, all tasks are returned in their original creation order. `GET /tasks/<task_id>` includes `turns`, `history_incomplete`, and `can_continue`. The latter indicates an eligible task with a recorded session ID; availability of its session file is checked when continuing. `POST /tasks/<task_id>/continue` accepts a JSON `message`.

Each new exchange stores its user message, timestamps, status, and response in the task's `turns` list. Prompts, final output, backups, and the list of changed files are stored separately under `<task_root>/<task_id>/turns/<turn_id>/`. The task-level result and existing result events continue to describe the latest exchange. Old tasks are adapted from their available prompt, replies, and last result, with a notice that earlier responses may be missing. History is retained until its files are removed; there is no automatic expiry, except for [backups](#backups).

For local browser checks, run `python codex-cli-worker/tests/web_fixture.py` from the repository root, then `node codex-cli-worker/tests/browser_smoke.cjs` with Playwright available. The fixture uses temporary storage and simulated responses, and never launches Codex or contacts Home Assistant. Set `CODEX_CHAT_BROWSER=msedge` to use installed Edge, and optionally `CODEX_CHAT_QA_DIR` to choose a screenshot directory outside the repository.
