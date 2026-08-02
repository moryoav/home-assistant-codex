# Codex for Home Assistant
[![HACS][hacs-badge]][hacs-url] [![release][release-badge]][release-url] ![downloads][downloads-badge] [![build][build-badge]][build-url] [![license][license-badge]][license-url]

Run Codex CLI against your Home Assistant configuration folder from Home Assistant.

## Example Flow

Ask Home Assistant Assist to make a configuration or dashboard change:

![Home Assistant Assist starting a Codex task to update a dashboard card label][assist-screenshot]

Codex runs the task and sends a Home Assistant notification when it finishes:

![Home Assistant persistent notification confirming a completed Codex task][notification-screenshot]

This repository contains two pieces:

- `codex-cli-worker`: a Home Assistant app/add-on that runs Codex CLI with read-write access to `/config`.
- `custom_components/codex_cli`: a Home Assistant custom integration that exposes entities and actions for starting tasks, checking status, signing in, cancelling work, and replying when Codex needs input.

## What It Does

- Starts Codex tasks from Home Assistant actions, scripts, automations, or Assist/LLM tools.
- Mounts the Home Assistant config folder as `/config` inside the worker app.
- Runs tasks non-interactively and stores task logs/results under `/config/codex_tasks`.
- Supports Codex device-code sign-in through Home Assistant persistent notifications.
- Reports active task count, latest task status, auth status, and task-running state.
- Uses Home Assistant Ingress for the app UI; the worker HTTP port is not exposed to the LAN.

## Security Notes

This app is powerful by design. It gives Codex access to your Home Assistant config folder so it can edit automations, scripts, dashboards, integrations, and related files.

The packaged worker follows the current Home Assistant app security guidance:

- No published LAN port.
- Ingress enabled for the web UI.
- Exact Ingress proxy source validation.
- Token-protected worker API for non-Ingress calls.
- AppArmor enabled with a custom profile.
- No Docker API access.
- No host network.
- No host PID/UTS access.
- No `full_access`.
- No elevated Supervisor role.

The `/config` mount is intentionally read-write because editing Home Assistant configuration is the core purpose of the project.

## Disclaimer

Use this project at your own risk. Codex for Home Assistant can read and modify your Home Assistant configuration, dashboards, scripts, automations, and related files. AI-generated changes can be incomplete, incorrect, unsafe, or incompatible with your setup.

You are solely responsible for reviewing changes, keeping backups, testing safely, and deciding whether to run this tool in your environment. The maintainer is not responsible or liable for damage, data loss, service interruption, security issues, misconfiguration, broken automations, device behavior, or any other loss or claim arising from installing, configuring, or using this project.

This project is provided "as is", without warranty of any kind, express or implied. See the repository license for the full warranty and liability disclaimer.

## Supported Architectures

Prebuilt app images are published for:

- `amd64`
- `aarch64`

## Installation

### 1. Add the Worker App Repository

[![Add the Codex app repository to Home Assistant](https://my.home-assistant.io/badges/supervisor_add_addon_repository.svg)](https://my.home-assistant.io/redirect/supervisor_add_addon_repository/?repository_url=https%3A%2F%2Fgithub.com%2Fmoryoav%2Fhome-assistant-codex)

Use the button above to add this repository to Home Assistant's Apps store.

If you prefer to do it manually:

In Home Assistant:

1. Go to **Settings** -> **Apps**.
2. Select **Install app**.
3. Open the menu in the top right.
4. Choose **Repositories**.
5. Add this repository URL:

```text
https://github.com/moryoav/home-assistant-codex
```

### 2. Install and Start the Worker App

[![Open the Codex CLI Worker app page](https://my.home-assistant.io/badges/supervisor_addon.svg)](https://my.home-assistant.io/redirect/supervisor_addon/?addon=8a8d906b_codex_cli_worker&repository_url=https%3A%2F%2Fgithub.com%2Fmoryoav%2Fhome-assistant-codex)

Use the button above after adding the repository. It opens the **Codex CLI Worker** app page.

1. Install **Codex CLI Worker**.
2. Review the app options.
3. Start the app.

The app keeps an internal worker API token in private app storage. The **Codex** integration provisions and rotates that token automatically through Home Assistant's Supervisor-managed app stdin, so you do not need to view, copy, or configure a token.

The app's web UI is available through Home Assistant Ingress. Do not try to open port `9123` directly; it is intentionally not exposed.

The app options include dropdowns for the Codex model and model reasoning effort. `medium` reasoning is the default balance; `high` and `xhigh` can spend more time/quota, and `xhigh` only applies where the selected model supports it.

The app web UI can view and save `/config/AGENTS.md`. You can also set the masked `HA_TOKEN` option when Codex tasks need a Home Assistant token in their environment.

### 3. Sign In to Codex

Before starting sign-in, enable **Enable device code authorization for Codex** in ChatGPT:

1. Open the ChatGPT website.
2. Click your profile.
3. Go to **Settings** -> **Security**.
4. Turn on **Enable device code authorization for Codex** near the bottom of the page.

This setting is in ChatGPT, not the Codex website.

Open the app web UI and click **Start login**, or call the `codex_cli.start_login` action after the integration is installed.

The app sends a Home Assistant persistent notification with:

- A QR code.
- A sign-in link.
- The one-time device code.

Scan the QR code or open the link, then enter the device code shown in the notification.

Codex CLI sign-in uses your ChatGPT/OpenAI account. It may work with a free ChatGPT account, but **ChatGPT Plus or higher is recommended** for more reasonable usage limits. This project does not use OpenAI API keys for Codex tasks.

### 4. Install the Integration

#### HACS

[![Open the Codex HACS repository](https://my.home-assistant.io/badges/hacs_repository.svg)](https://my.home-assistant.io/redirect/hacs_repository/?owner=moryoav&repository=home-assistant-codex&category=integration)

Codex is available in the default HACS catalog, so no custom repository setup is required.

1. Select the button above, or open HACS and search for **Codex** under **Integrations**.
2. Select **Codex** and choose **Download**.
3. Restart Home Assistant.

#### Manual

Copy:

```text
custom_components/codex_cli
```

to:

```text
/config/custom_components/codex_cli
```

Then restart Home Assistant.

### 5. Add the Integration

[![Add the Codex integration](https://my.home-assistant.io/badges/config_flow_start.svg)](https://my.home-assistant.io/redirect/config_flow_start/?domain=codex_cli)

Use the button above after Home Assistant restarts. It opens the **Codex** integration setup flow.

In Home Assistant:

1. Go to **Settings** -> **Devices & services**.
2. Add integration **Codex**.

The integration auto-detects the installed worker app and provisions its internal worker API token. There is no worker URL or API token to enter.

## Actions

The integration exposes these Home Assistant actions:

- `codex_cli.start_task`
- `codex_cli.start_login`
- `codex_cli.logout`
- `codex_cli.get_login_status`
- `codex_cli.list_tasks`
- `codex_cli.get_task`
- `codex_cli.cancel_task`
- `codex_cli.reply_task`

The integration also provides diagnostic sensors for auth status, active tasks, last task, Codex 5-hour and weekly usage limits, and separate 5-hour/weekly reset timestamp sensors. The usage sensors expose numeric percent states plus ISO datetime reset attributes when Codex reports reset times in `/status`.

When a Codex task completes, fails, or needs input, the worker fires a Home Assistant event named `codex_cli_task_result` in addition to the notification. Automations can listen for that event and read fields such as `task_id`, `status`, `summary`, `question`, `details`, and the nested `response` object from `trigger.event.data`.

Example:

```yaml
action: codex_cli.start_task
data:
  prompt: Check my Home dashboard for broken cards and suggest fixes.
response_variable: codex_result
```

## Assist / LLM Script Example

See [examples/scripts.yaml](examples/scripts.yaml) for a starter script you can expose to Assist or another LLM integration.

## Stable And Dev Branches

Use the default repository URL for stable releases:

```text
https://github.com/moryoav/home-assistant-codex
```

Development and canary builds are tested on the `dev` branch before being merged to `main`:

```text
https://github.com/moryoav/home-assistant-codex#dev
```

Only use the `dev` branch if you want to test changes before they are released to regular users. Stable users should stay on the default repository URL.

## Publishing Images

The repository includes a GitHub Actions workflow that builds and publishes multi-architecture images to GitHub Container Registry:

```text
ghcr.io/moryoav/codex-cli-worker
```

The app `config.yaml` points at that image. Home Assistant uses the app version as the image tag.

Release builds also publish the `latest` image tag. Manual workflow builds from development branches publish the app version and branch name tags, but do not move `latest`.

## Development Layout

```text
.
├── codex-cli-worker/
│   ├── config.yaml
│   ├── Dockerfile
│   ├── server.py
│   └── ...
├── custom_components/
│   └── codex_cli/
├── examples/
├── repository.yaml
└── hacs.json
```

[assist-screenshot]: https://raw.githubusercontent.com/moryoav/home-assistant-codex/main/examples/codex-1.JPG
[notification-screenshot]: https://raw.githubusercontent.com/moryoav/home-assistant-codex/main/examples/codex-2.JPG
[hacs-badge]: https://img.shields.io/badge/HACS-Default-41BDF5.svg?style=flat-square
[hacs-url]: https://github.com/hacs/integration
[release-badge]: https://img.shields.io/github/v/release/moryoav/home-assistant-codex?style=flat-square
[release-url]: https://github.com/moryoav/home-assistant-codex/releases
[downloads-badge]: https://img.shields.io/github/downloads/moryoav/home-assistant-codex/total?style=flat-square
[build-badge]: https://img.shields.io/github/actions/workflow/status/moryoav/home-assistant-codex/build.yaml?branch=main&style=flat-square&label=build
[build-url]: https://github.com/moryoav/home-assistant-codex/actions/workflows/build.yaml
[license-badge]: https://img.shields.io/github/license/moryoav/home-assistant-codex?style=flat-square
[license-url]: https://github.com/moryoav/home-assistant-codex/blob/main/LICENSE
