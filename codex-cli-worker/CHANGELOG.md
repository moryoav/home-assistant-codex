# Changelog

## Next

- Pin and verify the Codex CLI executable during both architecture builds.
- Disable interactive Codex update prompts in the worker-managed configuration.
- Use the fixed Codex executable path and fail tasks cleanly when it cannot be started.

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
