# Codex

Codex connects Home Assistant to the local Codex CLI Worker add-on. It lets Home Assistant start Codex tasks against the configuration folder, monitor active work, reply to tasks that need input, and start the Codex sign-in flow.

## Installation

Use the My Home Assistant buttons in the repository README for the fastest setup.

1. Add `https://github.com/moryoav/home-assistant-codex` as a Home Assistant app repository.
2. Install and start the Codex CLI Worker app.
3. Open HACS, search for **Codex** under **Integrations**, select it, and choose **Download**.
4. Restart Home Assistant.
5. Add the **Codex** integration from **Settings** > **Devices & services**.

As a manual fallback, copy `custom_components/codex_cli` into `/config/custom_components/codex_cli`, restart Home Assistant, and then add the integration from **Settings** > **Devices & services**.

## Configuration

The integration auto-detects the installed Codex CLI Worker app through the local Supervisor API. It also provisions the worker's private API token through Supervisor-managed app stdin, keeps the worker token in memory, and stores only the non-secret worker URL in the Home Assistant config entry.

There is no worker URL or API token to enter.

## Entities

- Auth status: Shows whether Codex CLI is signed in.
- 5-hour limit: Shows the latest Codex interactive usage line for the 5-hour window.
- Weekly limit: Shows the latest Codex interactive usage line for the weekly window.
- Active tasks: Shows the number of currently running Codex tasks.
- Last task: Shows the latest known task status and related attributes.
- Task running: Binary sensor that is on while a task is active.

All entities are diagnostic entities on the Codex device.

## Actions

- `codex_cli.start_task`: Start a Codex task. Requires `prompt`.
- `codex_cli.start_login`: Start the Codex sign-in flow; optional `force`.
- `codex_cli.logout`: Remove saved Codex CLI credentials from the worker.
- `codex_cli.get_login_status`: Return current sign-in status.
- `codex_cli.list_tasks`: Return known tasks.
- `codex_cli.get_task`: Return one task by task ID.
- `codex_cli.cancel_task`: Cancel one task by task ID.
- `codex_cli.reply_task`: Send a reply to a waiting task.

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

- If entities are unavailable, check that the Codex CLI Worker add-on is running and that the worker URL is reachable.
- If setup cannot connect, restart the Codex CLI Worker app so it can generate its worker API token, then reload or add the integration again.
- If Codex is not signed in, run `codex_cli.start_login` or use the add-on web UI to start the sign-in flow.
- If a task needs input, use `codex_cli.reply_task` with the task ID and reply text.

## Removal

1. Delete the Codex integration from Settings > Devices & services.
2. Disable or uninstall the Codex CLI Worker add-on if it is no longer needed.

## Known Limitations

- This integration controls a single local Codex CLI Worker instance.
- It depends on the worker add-on for task execution, Codex authentication, and notification delivery.
- Usage-limit sensors are best-effort values parsed from interactive Codex `/status` output and can temporarily be unavailable.
