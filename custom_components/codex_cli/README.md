# Codex integration

Codex connects Home Assistant to the local Codex CLI Worker app. It lets Home Assistant start Codex tasks against the configuration folder, monitor active work, reply to tasks that need input, and start the Codex sign-in flow.

## Installation

Use the My Home Assistant buttons in the repository README for the fastest setup.

1. Add `https://github.com/moryoav/home-assistant-codex` as a Home Assistant app repository.
2. Install and start the Codex CLI Worker app.
3. Open the app web UI and choose **Settings > Sign in** to sign in to Codex. See the [sign-in prerequisites](../../README.md#3-sign-in-to-codex).
4. Open HACS, search for **Codex** under **Integrations**, select it, and choose **Download**.
5. Restart Home Assistant.
6. Add the **Codex** integration from **Settings** > **Devices & services**.

As a manual fallback, copy `custom_components/codex_cli` into `/config/custom_components/codex_cli`, restart Home Assistant, and then add the integration from **Settings** > **Devices & services**.

## Configuration

The integration auto-detects the installed Codex CLI Worker app through the local Supervisor API. It also provisions the worker's private API token through Supervisor-managed app stdin, keeps the worker token in memory, and stores only the non-secret worker URL in the Home Assistant config entry.

There is no worker URL or API token to enter.

## Entities

- Auth status: Shows whether Codex CLI is signed in.
- 5-hour limit: Shows the percentage remaining in the 5-hour window when Codex reports one. The entity remains for compatibility and is `unknown` with `reported: false` when that window is omitted.
- 5-hour reset: Shows when the 5-hour window resets, if a reset time can be parsed.
- Weekly limit: Shows the percentage remaining in the weekly window when Codex reports one.
- Weekly reset: Shows when the weekly window resets, if a reset time can be parsed.
- Active tasks: Shows the number of currently running Codex tasks.
- Last task: Shows the latest known task status and related attributes. When that task failed, the `error` attribute gives the reason.
- Task running: Binary sensor that is on while a task is active.

All entities are diagnostic entities on the Codex device.

Reset sensors are `unknown` when no reset time is available. Usage sensors keep the original reset description in `reset_text` and expose a parsed ISO datetime in `reset` when available.

## Actions

- `codex_cli.start_task`: Start a Codex task. Requires `prompt`.
- `codex_cli.continue_task`: Continue a saved task. Requires `task_id` and `message`. Supports completed, failed, cancelled, and waiting tasks with an available Codex session.
- `codex_cli.start_login`: Start the Codex sign-in flow; optional `force`.
- `codex_cli.logout`: Remove saved Codex CLI credentials from the worker.
- `codex_cli.get_login_status`: Return current sign-in status.
- `codex_cli.list_tasks`: Return all tasks or filter with `limit`, `offset`, `status`, `order`, and `summary`. Use `order: updated_desc` for recent activity and `summary: true` for compact entries.
- `codex_cli.get_task`: Return one task by task ID, including its conversation in `turns`.
- `codex_cli.cancel_task`: Cancel one task by task ID.
- `codex_cli.reply_task`: Send a reply to a waiting task. Requires `task_id` and `reply`. With the optional `turn_id`, taken from the `codex_cli_task_result` event or the **Last task** sensor, the reply is refused once that question is no longer the one waiting.

These actions do not queue messages. Starting or continuing a task while another task is active or messages are already queued returns an error; the worker chat UI provides the queue.

Example automation action:

```yaml
action: codex_cli.start_task
data:
  prompt: Can you inspect my Home dashboard and report any obvious issues?
response_variable: codex_result
```

## Data Updates

The integration polls the worker every 30 seconds. Actions that start, cancel, or reply to tasks request an immediate refresh after the worker responds.

## Troubleshooting

- If entities are unavailable, check that the Codex CLI Worker app is running. The integration discovers its internal URL automatically.
- If the app's web UI says that the integration is not installed, out of date, or not connected, do what the line says: install or update the integration, restart Home Assistant, and add it under **Settings** > **Devices & services**.
- If setup cannot connect, restart the Codex CLI Worker app, then reload or add the integration again so it can provision a fresh worker API token.
- If Codex is not signed in, run `codex_cli.start_login` or choose **Settings > Sign in** in the app web UI.
- If a task needs input, use `codex_cli.reply_task` with the task ID and reply text. The answers Codex offered are in the `choices` attribute of the **Last task** sensor and in the `codex_cli_task_result` event.
- If the worker's `notify_service` is a mobile app service, a question's notification has a button for each of those answers. The integration sends the tapped one to the worker. If the worker does not take it, for example because the question was already answered, a persistent notification says why.

## Removal

1. Delete the Codex integration from **Settings > Devices & services**.
2. Stop or uninstall the Codex CLI Worker app if it is no longer needed.

## Known Limitations

- This integration controls a single local Codex CLI Worker instance.
- It depends on the worker app for task execution, Codex authentication, and notification delivery.
- Usage-limit sensors are best-effort values parsed from interactive Codex `/status` output. Reported windows vary by account and plan; omitted windows remain `unknown` with `reported: false`.
