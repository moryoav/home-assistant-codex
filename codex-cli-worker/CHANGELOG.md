# Changelog

## 0.1.55

- **The remembered chat opens immediately.** In 0.1.54 the welcome screen showed for a moment on each load while the chat list was fetched, then switched to the remembered chat. The web UI now opens that chat before anything else loads, so the switch is gone. A remembered chat that no longer exists still falls back to a new chat without an error.

Update the **Codex CLI Worker** app to **0.1.55** for the smoother return to your chat.

## 0.1.54

- **The chat you left open is reopened when you return.** Switching to another app on a phone often reloads the panel, which used to land on the welcome screen every time. The web UI now remembers the open chat in the browser and reopens it on the next load. Choosing **New chat** is remembered as well, so the next visit starts fresh.
- If the remembered chat was deleted in the meantime, from another tab or device, the UI opens a new chat without showing an error. The choice is kept in the browser only; the worker does not record which chat you had open.

Update the **Codex CLI Worker** app to **0.1.54** to return to the chat you left open.

## 0.1.53

- **See what Codex is doing while it works.** The chat now shows a live activity list under your latest message: reasoning headlines, progress notes, the commands Codex runs, the files it edits, web searches, and tool calls appear as they happen. Failed commands and errors are highlighted, and a stopped or failed run ends with a **Stopped** or **Failed** row.
- The list stays with the latest exchange after it finishes, collapsed behind **Show activity (N steps)**, and is kept across page reloads. Earlier exchanges do not show steps.
- Command output is not shown by default. Each command offers **Show output** for the first 2 KB, redacted like the task log, with a marker when it was cut. File edits show paths only.
- New add-on option `reasoning_summary` (`concise` by default, or `detailed` or `none`). Codex only reports reasoning when summaries are requested, so the default turns them on. `none` keeps commands and progress notes in the list but hides reasoning. The option is passed to `codex exec` as `--config model_reasoning_summary`.
- Worker API: `GET /tasks/<task_id>/activity?after=<seq>` returns the steps of the latest exchange added since a sequence number, with `running`, `turn_id`, `seq`, and `total`. The web UI polls it once a second while a chat is working. Steps are saved to `turns/<turn_id>/activity.json` when the run ends; at most 500 steps are kept per exchange.

Update the **Codex CLI Worker** app to **0.1.53** to watch Codex work in the chat.

## 0.1.52

- Fix attaching images from the Home Assistant Android app. The app's file chooser returns nothing when the system Photo Picker is opened in multi-select mode, so the paperclip now asks Android WebViews for a single image; use it again to add more. Paste and drag-and-drop still accept several images at once.
- Report why an attached image was not added instead of doing nothing. A file the picker hands over empty or unreadable, a pick made while a message is still sending, and any unexpected failure while preparing an image now show a message above the composer.
- Read images whose size the picker reports as zero before giving up, since some mobile pickers only reveal the content when it is read.
- Do not rely on `crypto.randomUUID` being available; it is missing over plain HTTP in some mobile browsers.

Update the **Codex CLI Worker** app to **0.1.52** to see why an image could not be attached on a phone.

## 0.1.51

- **Attach images to a message.** Use the paperclip button next to the model pill, paste a screenshot into the message box, or drop image files onto the composer. Attached images are sent to Codex with the message, shown with your message in the chat, and kept with the conversation. Works for new chats and for continuing saved ones.
- Images reach Codex through the CLI `--image` option and are named in the task prompt. PNG, JPEG, GIF, and WebP only; up to 6 images per message and 10 MB each. The UI shrinks images larger than 2048 pixels on their longest edge before uploading.
- Worker API: `POST /tasks`, `POST /tasks/<task_id>/continue`, and `POST /tasks/<task_id>/reply` accept an optional `attachments` list of base64 images. Each turn reports its uploads under `prompt_attachments`, attachment entries carry an `origin` of `user` or `codex`, and `GET /tasks/<task_id>/attachments/<attachment_id>` serves both kinds.
- Replace the reasoning level's text arrow with an aligned chevron icon.

Update the **Codex CLI Worker** app to **0.1.51** to attach images to chats.

## 0.1.50

- Add a chat actions menu to the sidebar. On desktop, hover over a chat and use its **⋯** button, or right-click the row; on phones, long-press the chat to open a bottom sheet. The menu also opens from the keyboard with Shift+F10.
- **Pin** chats to keep them at the top of the list under a **Pinned** heading, and unpin them again. Pins persist across reloads and worker restarts.
- **Rename** chats from a dialog. The new title appears in the sidebar and the conversation header without moving the chat in the recent list.
- **Delete** chats after a confirmation. This removes the task directory, the Codex session it could resume, its generated images, and the task index entry. A running chat must be stopped first.
- Worker API: `POST /tasks/<task_id>/pin`, `POST /tasks/<task_id>/title`, `DELETE /tasks/<task_id>`, the `pinned_first` order for `GET /tasks`, and a `pinned` flag on summary entries.

Update the **Codex CLI Worker** app to **0.1.50** to pin, rename, and delete chats from the sidebar.

## 0.1.49

- Show images generated by Codex directly in the chat. When a conversation asks for an image, the worker captures the built-in image generation result, stores a copy with that exchange, and shows it under the response with a full-size link and a **Download** button.
- Keep generated images with their conversation across page reloads and worker restarts, including when a saved chat is continued. Each exchange only shows the images it produced.
- Serve images only through the authenticated worker API at `GET /tasks/<task_id>/attachments/<attachment_id>`. The worker verifies the stored file, its image type, and a 25 MB size limit before sending it.
- Include attachment metadata in `GET /tasks/<task_id>`, `codex_cli.get_task`, and the `codex_cli_task_result` event so automations can reference generated images.
- Tell Codex that generated images are attached automatically, so it does not need to copy them into `/config` unless a specific file location is requested.

Update the **Codex CLI Worker** app to **0.1.49** to see generated images in chats.

## 0.1.48

- Add a compact model picker and a blue reasoning slider below the message box, with rounded menus and a checkmark for the selected model.
- Save model and reasoning choices separately for each conversation, including across worker restarts. Changes apply to the next message while preserving the conversation's context.
- Keep new chats on the add-on defaults, with independent controls to reset the model or reasoning selection.
- Show reasoning levels supported by the selected model, including Max and Ultra where available. Reset an incompatible reasoning selection when switching models.
- Support keyboard navigation, small screens, and light and dark themes for both pickers.

Update the worker app to **0.1.48** to use per-conversation model and reasoning selection.

## 0.1.47

- Show remaining **5h** and **7d** account quota percentages below **Home Assistant workspace** in the web UI sidebar.
- Refresh quota automatically and show reset times on hover when available.
- Identify cached quota while a task is running and show unavailable values clearly.

Update the **Codex CLI Worker** app to see the new quota display.

## 0.1.46

### New chat web UI

Introduce a full chat interface for Codex in Home Assistant, inspired by ChatGPT and Codex. Browse earlier conversations, read their messages and responses, and pick up where you left off without starting a new task every time.

- Browse saved chats in a **resizable left sidebar**, with the selected conversation open on the right.
- **Continue a previous conversation with its saved context**, even after the task has completed, or choose **New chat** to start fresh.
- Read user messages and Codex responses together in a conversation view, with history preserved across worker restarts.
- Use a **mobile-friendly layout** with a conversation drawer, plus light and dark themes.
- Keep account sign-in and workspace instructions together in **Settings**.

### Home Assistant actions and history

- Add `codex_cli.continue_task` for follow-up messages from scripts and automations.
- Add task-list filters, pagination, and compact summaries while preserving existing action defaults.
- Preserve each exchange's messages, responses, snapshots, and change manifests.
- Show available history for older tasks and report a clear error if their saved Codex session is missing.

Update both the **Codex CLI Worker** app and the **Codex** HACS integration to **0.1.46**, then restart Home Assistant to register the new action. Existing tasks remain available; earlier responses already overwritten by older versions cannot be recovered.

## 0.1.45

- Fix the sandbox readiness check added in 0.1.44. It called `codex sandbox linux ...`, but the bundled Codex CLI 0.154.0 has no platform subcommand, so the check tried to run a program named `linux`, reported `Failed to execvp linux`, and blocked every task in the `read-only` and `workspace-write` modes. The check now runs `codex sandbox [options] -- /bin/true`.

## 0.1.44

- Work around an upstream Codex CLI bug ([openai/codex#44329](https://github.com/openai/codex/issues/44329)) that made every sandboxed task fail with `bwrap: Can't mount proc on /proc: Operation not permitted` on hosts that deny a fresh `/proc` mount, including Home Assistant OS. Codex now selects its own no-proc fallback again. This is a temporary, narrowly scoped workaround for the `read-only` and `workspace-write` modes; `danger-full-access` is unchanged.
- Verify a real Codex sandbox execution before reporting the sandbox as ready, so a broken sandbox is reported at startup and before each task instead of failing mid-task.
- See `SANDBOX_TESTING.md` for the scope, verification steps, and how to remove the workaround once upstream is fixed.

## 0.1.43

- Add GPT-6 Astra to the Codex model selector.
- Update the bundled Codex CLI to 0.154.0 for Astra support.
- Document Astra availability and supported reasoning choices.

## 0.1.42

- Add GPT-5.6 Sol, Terra, and Luna to the Codex model selector.
- Remove old and deprecated model choices while retaining GPT-5.5 as a previous-generation fallback.
- Preserve upgrade compatibility for existing GPT-5.3-Codex selections by treating them as the CLI default.

## 0.1.41

- Document how to request optional Markdown-friendly task output through `/config/AGENTS.md` without changing the default worker output contract.

## 0.1.40

- Record task session IDs only from Codex `thread.started` events, retain the requested session as a fallback, and prevent stale reply output from being reused.

## 0.1.39

- Let the installed Codex CLI choose its recommended model by default and migrate the legacy GPT-5.3-Codex selection safely.
- Update Codex CLI to 0.146.0 and make `workspace-write` and `read-only` sandboxing work on HAOS with a capability-dropping Bubblewrap wrapper and a nested AppArmor setup profile.
- Add Codex version and sandbox readiness diagnostics, including the supported no-proc fallback for restrictive containers.
- Harden task launch, cancellation, timeout, and child-process cleanup paths.
- Handle account-specific usage windows, keep the existing 5-hour entities compatible when only a weekly window is reported, and redact account/session data from diagnostic excerpts.

## 0.1.38

- Pin and verify the Codex CLI executable during both architecture builds.
- Disable interactive Codex update prompts in the worker-managed configuration.
- Use the fixed Codex executable path and fail tasks cleanly when it cannot be started.

## 0.1.37

- Updated installation documentation for availability in the default HACS catalog.

## 0.1.36

- Replaced the option-backed `AGENTS.md` writer with an add-on web UI editor for the real `/config/AGENTS.md` file.
- Kept `HA_TOKEN` as the optional masked add-on option passed to Codex subprocesses.

## 0.1.35

- Added optional add-on configuration for writing `/config/AGENTS.md` before Codex runs.
- Added a masked optional `HA_TOKEN` add-on option and pass it to Codex subprocesses as an environment variable.

## 0.1.34

- Added HACS and Hassfest validation workflows required for HACS default repository submission.
- Fixed HACS manifest validation by removing an unsupported legacy key.
- Fixed Hassfest manifest ordering and declared the integration as config-entry-only.
- Added GitHub repository topics required by HACS repository checks.

## 0.1.33

- Updated README badges and screenshot links so HACS can render the project images reliably.
- Added dark-theme brand assets for the HACS and Home Assistant integration views.
- Aligned the custom integration manifest version with the release version.

## 0.1.32

- Added separate timestamp sensors for Codex 5-hour and weekly reset times.
- Fire a `codex_cli_task_result` Home Assistant event whenever a Codex task completes, fails, or needs input. The event data includes the final Codex response and task metadata.

## 0.1.31

- Expose usage sensor `reset` attributes as ISO datetime strings and keep Codex's original human text in `reset_text` attributes.

## 0.1.30

- Preserve reset times from the rich Codex `/status` panel when a later TUI footer redraw also contains 5-hour and weekly percentages without reset text.

## 0.1.29

- Submit the Codex TUI `/status` command as typed text followed by a delayed Enter keypress.
- Fix usage probing when Codex accepted `/status` into the prompt line but did not execute it, which prevented the rich status panel and reset times from being captured.
- Add `reset_at` ISO timestamp attributes derived from Codex's reported reset times.

## 0.1.28

- Wait for the rich Codex `/status` panel before finishing the usage probe.
- Keep footer status-line quota values as a fallback when the rich panel does not appear.

## 0.1.27

- Make the 5-hour and weekly usage sensors numeric percentage sensors.
- Parse reset text from richer Codex `/status` output when available and expose it as sensor attributes.

## 0.1.26

- Parse concise Codex usage values from TUI redraw output instead of storing an overlong terminal line.
- Keep usage sensor states short enough for Home Assistant state values.

## 0.1.25

- Detect Codex CLI's trust-directory prompt even when TUI control sequences remove spacing.
- Wait briefly after accepting directory trust before sending `/status`.
- Request Codex's five-hour and weekly limit status-line items during the usage probe.

## 0.1.24

- Handle Codex CLI's trust-directory prompt before sending `/status` in the usage probe.
- Parse the full captured TUI output when looking for usage lines.
- Expose the usage probe `raw_excerpt` on the Home Assistant usage sensors for troubleshooting.

## 0.1.23

- Added best-effort interactive usage probing through a pseudo-terminal to collect Codex `/status` usage lines.
- Exposed parsed 5-hour and weekly usage lines in worker `/status` responses.
- Added integration sensors for 5-hour and weekly usage status lines.
- Refresh usage status on startup (when logged in), after tasks finish, after login completes, and via periodic status polling.
- Hardened the usage probe so it uses a stable terminal size, defers while tasks are running, and preserves captured output if the TUI exits quickly.

## 0.1.22

- Added logout support through the worker web UI and the `codex_cli.logout` Home Assistant action.
- Improved Codex sign-in QR rendering by adding an explicit white QR background for dark Home Assistant themes.
- Added a documented `dev` branch workflow for canary testing before stable releases.
- Updated image publishing so manual branch builds do not move the `latest` image tag.
- Improved worker discovery when stable and dev worker apps are installed side by side.

## 0.1.21

- Ignore binary Home Assistant storage helper files such as `.pickle` and `.pkl` when building task snapshots.
- Do not fail completed Codex tasks when unrelated binary `.storage` files change during validation.

## 0.1.20

- Added the `model_reasoning_effort` app option with a dropdown for `minimal`, `low`, `medium`, `high`, and `xhigh`.
- Pass the selected reasoning effort to every `codex exec` run using the per-run Codex CLI config override.
- Documented the reasoning effort choices and their speed/quota tradeoffs.

## 0.1.19

- Clarified that ChatGPT Free may work, but ChatGPT Plus or higher is recommended for more practical Codex usage limits.
- Added Assist workflow screenshots to the README.

## 0.1.18

- Improved the README installation flow with contextual Home Assistant and HACS buttons.
- Added a direct sign-in link below the QR code so Codex authentication can be completed from one device.
- Simplified task starts so prompts no longer require a separate title.

## 0.1.17

- Always provision a fresh in-memory worker API token from the integration instead of reusing stale legacy app option values.

## 0.1.16

- Provision the internal worker API token through Supervisor-managed app stdin.
- Keep the worker API token out of both the app configuration UI and the integration config entry.

## 0.1.15

- Moved the worker API token from user-visible app options into private app storage.
- Added secure token bootstrap for the Home Assistant Codex integration.
- Removed duplicate image builds on normal pushes; GHCR images now publish on releases or manual workflow runs.

## 0.1.14

- Advertise the worker app through Supervisor discovery for the Codex integration.
- Document automatic worker connection, Codex device-code prerequisites, and subscription requirements.

## 0.1.13

- Broadened the custom AppArmor profile so the Python/Node worker can start while keeping AppArmor enabled.

## 0.1.12

- Removed the default LAN port publication so the web UI is accessed through Home Assistant Ingress.
- Added exact Ingress proxy source validation in the worker server.
- Blocked direct non-Ingress access to the web UI.
- Required worker API authentication for `/health`.
- Added constant-time worker API token comparison.
- Added a custom AppArmor profile.
- Added README and expanded security documentation.

## 0.1.11

- Added generated worker API token support.
- Added Codex device-code sign-in notifications.
- Added task execution and status APIs for the Home Assistant Codex integration.
