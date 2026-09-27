# Home Assistant verification

Update both the worker and the Codex integration to use authenticated diagnostics
and the bundled dashboard browser. No Home Assistant password or manually created
long-lived token is needed. These tools do not need the optional `HA_TOKEN` setting.

## Checks and evidence

- YAML changes retain the existing syntax and Home Assistant configuration checks.
- Entity checks fetch fresh state and up to ten explicitly named attributes. An
  expected state can be compared exactly. Run checks after any authorized reload;
  matching state does not prove automation triggers, conditions, or actions work.
- Core logs and the last 100 lines of a specified app's logs are available, capped
  at 32 KiB. Known credential patterns are redacted before persistence/model access.
  Logs can still contain personal information.
- Saved storage dashboards get a fresh WebSocket configuration readback compared
  with the edited configuration, followed by desktop (1440 x 1000) and mobile
  (390 x 844) captures, within the turn limits.
- The AI can request particular dashboard views during a task, inspect the returned
  images, and revise its work. `save_pending: true` saves only a matching storage
  dashboard edited since this turn's baseline, with API readback before capture.
  This requires `auto_save_lovelace`. Without it, the browser inspects loaded state.
- YAML dashboards and additional views require an explicit path. Arbitrary YAML
  includes cannot reliably be mapped to dashboard URLs automatically.

Browser results include console errors, failed HTTP responses, blocked requests,
and visible error cards. Capturing screenshots is evidence for visual review, not
a guarantee of correct layout. API results distinguish `observed`, `passed`, and
`failed`; browser results distinguish `captured`, `issues`, `unavailable`, and
`disabled`. Problems stay visible even when the edit itself completes.

Each turn retains `verification` and `verification_attachments` in the task API,
`codex_cli.get_task`, and normal task-result events. The chat shows expandable
results and private screenshots. Automatic captures after the final answer are
labeled as captures, not images already inspected by the AI.

## Task tools

The installed `ha-verify` command sends one JSON request over a private Unix socket
using a temporary capability supplied only to the active task. There is no generic
URL, service-call, reload, click, or JavaScript evaluation tool.

```sh
ha-verify '{"operation":"entity","entity_id":"light.kitchen","expected_state":"on","attributes":["brightness"]}'
ha-verify '{"operation":"config_check"}'
ha-verify '{"operation":"logs","target":"core"}'
ha-verify '{"operation":"logs","target":"core_mosquitto"}'
ha-verify '{"operation":"dashboard_readback","path":"/lovelace/lights"}'
ha-verify '{"operation":"dashboard","path":"/lovelace/lights","save_pending":true}'
```

Use the exact installed app slug for logs. Omit `save_pending` for an existing
dashboard. Perform reloads/device actions only with the user's authorization,
then use a fresh readback. Do not equate an accepted command with a verified outcome.

## Automatic authentication

1. The integration creates a dedicated system user with read-only entity permissions
   and `local_only`, through Core's auth manager. It never adopts a human account
   or directly edits the auth storage file.
2. The worker uses its automatic Supervisor token to reach a narrow integration
   endpoint. The endpoint also requires the paired worker secret in the JSON body.
   A custom header would not survive the Supervisor proxy's header allowlist.
3. Core issues a 180-second access token and retains the renewal credential. The
   browser connects directly to local Core, preferring its internal Supervisor
   network address and respecting a configured TLS connection.
4. The external-authentication bridge supplies a placeholder to page scripts. The
   controller substitutes the real token only on approved Core WebSocket/REST
   requests. Authenticated REST redirects are blocked.
5. Completion revokes the session. Core independently revokes it after 180 seconds,
   including authenticated sockets. Reload removes orphaned credentials; removing
   the integration removes its identity.

No additional Supervisor role is granted to the worker. Entity/configuration access
uses its existing `homeassistant_api` permission. Log reads use a narrow integration
endpoint allowing only Core logs or a validated app slug, with bounded responses.

## Limits

- `browser_verification` defaults to true. Disable it to retain API diagnostics
  without browser work. Chromium is packaged for amd64 and aarch64. Missing or old
  integrations cause browser/log checks to report unavailable.
- Browser HTTP requests stay on the Core origin and known read paths. WebSocket
  messages use an explicit read allowlist. Service calls, saves, arbitrary event
  subscriptions, external sites, and service workers are blocked. Unsupported
  custom-card requests are reported instead of silently expanding access.
- The dedicated identity may see different cards, themes, or views from a household
  member. Administrator-only dashboards, external resources, and cards requiring
  writes during initialization may not be available.
- At most 24 checks and four browser runs per turn, one at a time. Each browser run
  has a 110-second deadline, a sampled 768 MiB combined process RSS budget, and two
  fixed-size images capped at 5 MiB each. The memory budget is not a kernel-enforced
  instantaneous ceiling. Cancellation/timeout terminate tracked browser descendants.
- Temporary profiles and captures are discarded after each run. Browser credentials
  are not passed as command-line arguments or saved in profiles.
- Saved screenshots expire after seven days, with deletion on the next worker
  startup, verification turn, or request for an expired image. Metadata stays in
  the conversation. Chat deletion removes screenshots too. Images use the existing
  authenticated attachment API and `Cache-Control: no-store`; household information
  visible on the dashboard is visible in the screenshots.

## Credential and recovery boundaries

Supervisor credentials are removed from AI subprocess environments. An explicitly
configured `HA_TOKEN` retains its opt-in behavior. Known credential files such as
`secrets.yaml`, auth stores, and `core.config_entries` remain in change detection
but are excluded from new snapshots. Existing snapshots are not rewritten. Those
files need Home Assistant backups for recovery. Other files can contain inline
secrets, so this is targeted protection, not complete secret detection.

Chromium uses the app's container/AppArmor confinement with its own sandbox disabled
because the app runs as root. It receives no Supervisor or worker credentials in its
environment. The worker, CLI, and browser share the container and filesystem; these
controls are not isolation against a compromised root process. Do not describe them
as complete credential isolation or a hardened browser sandbox.

## Validation

Run `pytest`. Tests under `tests/ha` use real Home Assistant authentication.
`node tests/browser_verification.cjs` uses real Chromium and a protocol fixture to
check viewports, external auth, blocked writes, broken resources/cards, rejected
authentication, and cleanup. Provide `playwright-core` and `ws` on `NODE_PATH` and
set `HA_BROWSER_EXECUTABLE` when Chromium is not at `/usr/bin/chromium-browser`.

Set `HA_BROWSER_NODE=node` to include the real Home Assistant frontend test under
`tests/ha`. CI also builds both container architectures. Fixture tests are not a
deployment test of a user's HAOS kernel, AppArmor policy, or installed custom cards.

References: [app authentication](https://developers.home-assistant.io/docs/apps/communication/),
[external authentication](https://developers.home-assistant.io/docs/frontend/external-authentication/),
and [WebSocket routing](https://playwright.dev/docs/api/class-websocketroute).
