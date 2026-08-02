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

`codex_model` is a fixed selection to avoid typo-prone free text. The default value, `default`, lets the installed Codex CLI choose its recommended model. The legacy `gpt-5.3-codex` selection is still accepted for upgrade compatibility but is treated as `default` because it is no longer supported with ChatGPT sign-in.

`model_reasoning_effort` controls how much reasoning Codex asks supported models to use for each non-interactive task. The app passes it to `codex exec` as a per-run `--config model_reasoning_effort="<value>"` override rather than writing it into `config.toml`. Available values are:

- `minimal`: fastest and lowest reasoning.
- `low`: lighter reasoning.
- `medium`: balanced default.
- `high`: more reasoning, usually slower and more quota-intensive.
- `xhigh`: highest effort where the selected model supports it; support is model-dependent.

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

Use the add-on web UI **Log out** button or the Home Assistant service `codex_cli.logout` to run `codex logout` and remove saved Codex CLI credentials from the worker. Logout is blocked while a Codex task is actively running.

The app uses the built-in Supervisor token for Home Assistant notifications and dashboard saves through the Home Assistant Core API proxy. No Home Assistant long-lived access token is required.

Codex CLI sign-in uses your ChatGPT/OpenAI account. It may work with a free ChatGPT account, but ChatGPT Plus or higher is recommended for more reasonable usage limits. This project does not use OpenAI API keys for Codex tasks.

## AGENTS.md and HA_TOKEN

The add-on web UI includes an editor for `/config/AGENTS.md`. This file remains the source of truth for shared Codex project instructions.

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
