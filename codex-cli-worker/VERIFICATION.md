# Home Assistant verification

Update both the worker and the Codex integration to use authenticated diagnostics
and the bundled dashboard browser. No Home Assistant password or manually created
long-lived token is needed. These tools do not need the optional `HA_TOKEN` setting.

## Checks and evidence

- YAML changes retain the existing syntax and Home Assistant configuration checks.
- Entity checks fetch fresh state and up to ten explicitly named attributes. An
  expected state can be compared exactly. Run checks after any authorized reload;
  matching state does not prove automation triggers, conditions, or actions work.
- The last 100 lines of Core logs are available, capped
  at 32 KiB. Known credential patterns are redacted before persistence/model access.
  Logs can still contain personal information or unrecognized secrets. Other apps'
  logs are not accessible through this tool.
- Saved storage dashboards get a fresh WebSocket configuration readback compared
  with the edited configuration, followed by desktop (1440 x 1000) and mobile
  (390 x 844) captures of the first views that fit the remaining turn budget.
  Request later affected views explicitly; automatic captures do not cover every view.
- The AI can request particular dashboard views during a task, inspect the returned
  images, and revise its work. `save_pending: true` saves only a matching storage
  dashboard edited since this turn's baseline, with API readback before capture.
  This requires `auto_save_lovelace` and a writable task mode. Without it, the browser inspects loaded state.
- YAML dashboards and additional views require an explicit path. Arbitrary YAML
  includes cannot reliably be mapped to dashboard URLs automatically.

Browser results include console errors, failed HTTP responses, blocked requests,
and visible error cards. Repeated findings include counts and affected viewports,
grouped as resource failures, Home Assistant errors, dashboard errors, blocked
actions, or blocked diagnostic logging/notifications. Diagnostic write attempts
remain blocked but do not alone mark a capture as having rendering issues.
Capturing screenshots is evidence for visual review, not
a guarantee of correct layout. API results distinguish `observed`, `passed`, and
`failed`; browser results distinguish `captured`, `issues`, `unavailable`, and
`disabled`. Problems stay visible even when the edit itself completes.

Each turn retains `verification` and `verification_attachments` in the task API,
`codex_cli.get_task`, and normal task-result events. The chat shows expandable
results and private screenshots. Automatic captures after the final answer are
labeled as captures, not images already inspected by the AI.

## Task tools

The worker registers a trusted `home_assistant` stdio MCP server for each task.
Its `verify` tool accepts the following JSON argument objects. The bridge connects
to the private worker socket outside the shell sandbox, so `workspace-write` and
`read-only` keep their existing network restrictions. The temporary capability is
forwarded to this process through an environment variable, never stored in config.
This bounded tool is approved for non-interactive calls; it does not grant general
shell network access, service calls, reloads, arbitrary URLs, clicks, or evaluation.

```json
{"operation":"entity","entity_id":"light.kitchen","expected_state":"on","attributes":["brightness"]}
{"operation":"config_check"}
{"operation":"logs"}
{"operation":"dashboard_readback","path":"/lovelace/lights"}
{"operation":"dashboard","path":"/lovelace/lights","save_pending":true}
```

Omit `save_pending` for an existing
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
   requests. Core HTTP redirects are blocked, including dashboard assets.
   For an HTTP Core address, the temporary browser treats only that exact origin
   as a secure context so Chromium can grant its local-network permission. This
   does not encrypt HTTP or disable certificate checks, and request restrictions
   still apply. Other origins receive no exception.
5. Completion revokes the session. Core independently revokes it after 180 seconds,
   including authenticated sockets. Reload removes orphaned credentials; removing
   the integration removes its identity.

No additional Supervisor role is granted to the worker. Entity/configuration access
uses its existing `homeassistant_api` permission. Log reads use a narrow integration
endpoint authorizing only bounded Core log reads. It uses Core's Supervisor token
for that operation; it does not allow other installed apps' logs.

## Dashboard resources

Version 0.1.61 requires both worker and integration updates, followed by a Home
Assistant restart, to discover custom integrations' static routes. Older brokers
still support the original built-in paths but cannot supply the new route list.

Core supplies registered static file and directory URLs, never filesystem paths.
This allows assets such as Browser Mod, custom icons, navigation modules, and
WebRTC card scripts without granting access to arbitrary integration GET endpoints.
Loading a script does not permit its service calls, writes, or unsupported APIs.
Custom Icons' active-set, list, cache, and single-icon reads are supported with
bounded set/icon names; icon selection and download operations remain blocked.
Browser Mod connection/registration and navigation-controller APIs remain
unsupported, so features depending on them may still report blocked requests.

Public HTTPS assets are fetched through a separate client that sends no Home
Assistant token, browser cookies, authorization, origin, or referrer headers.
Supported sources are:

- External frontend modules registered by Core and Lovelace resources returned by
  Core, plus static assets within each registered script or stylesheet directory.
- Google Fonts CSS and fonts, jsDelivr (`cdn` and `fastly`, npm/GitHub assets
  and npm `+esm` modules including their nested imports),
  cdnjs library assets, and unpkg assets with supported static file extensions.
- Font, image, and stylesheet URLs referenced by loaded CSS.

External navigation, non-HTTPS requests, URL credentials, custom ports, IP-literal
hosts, local names, and private/reserved DNS addresses are rejected. Each request
pins a validated public address while retaining TLS certificate checks; redirects
recheck both the URL policy and DNS. External responses cannot install cookies.
Requests are limited to 128 per capture, 8 MiB per response, 32 MiB total, three
redirects, and 15 seconds per resource including redirects. These limits operate
alongside the browser's overall time and memory budgets.

External servers can see the host's public IP address and resource URLs, including
their configured query strings. Dashboard code remains trusted content: custom
scripts run in the page and can see dashboard data. The restricted asset transport
is not a guarantee against data disclosure by a malicious installed card. Disable
the built-in browser to prevent browser captures and their resource requests.

Before capture, the browser waits up to six seconds for network idle and three
seconds for fonts, within the overall deadline. Dynamic cards can continue loading
afterward. Blocked logging/notification requests are reported as diagnostic notes;
other blocked service calls remain review findings. Unavailable entities, backend
HTTP errors, and invalid dashboard templates still require separate fixes.

## Limits

- **Enable built-in browser** (`browser_verification`) defaults to true. Browser
  captures use RAM while running. Turn it off to prevent browser launches and
  screenshots while retaining API diagnostics. Chromium is packaged for amd64 and aarch64. Missing or old
  integrations cause browser/log checks to report unavailable.
- Authenticated browser HTTP requests stay on the Core origin and known read paths. WebSocket
  messages use an explicit read allowlist. Service calls, saves, arbitrary event
  subscriptions, external navigation, and service workers are blocked. External
  assets use the separate restricted transport described above. Unsupported
  custom-card requests are reported instead of silently expanding access.
- The dedicated identity may see different cards, themes, or views from a household
  member. Administrator-only dashboards, unsupported external resources, and cards requiring
  writes during initialization may not be available.
- At most 24 checks and four browser runs per turn, one at a time. Each browser run
  has a 110-second deadline, a sampled process-tree memory budget, and two
  fixed-size images capped at 5 MiB each. **Browser memory limit (MiB)** in the app's
  Configuration tab (`browser_memory_limit_mib`) defaults to 1536 MiB (1.5 GiB)
  and accepts 512 to 8192 MiB. Raise it for larger dashboards when the host has
  enough available memory; lower it for smaller systems. It applies to each new
  capture and does not reserve RAM or limit the whole app. Linux proportional set size (PSS) counts
  shared Chromium pages proportionally instead of charging them once per process;
  proportional swap is also charged. RSS is a conservative fallback if PSS cannot
  be read. The budget includes Node and tracked Chromium descendants, and captured
  results record the measured peak. Fetched response bodies are released after
  delivery instead of retained until context close. The memory budget is not a
  kernel-enforced instantaneous ceiling. Cancellation/timeout terminate tracked
  browser descendants. Disable browser verification on systems without enough
  memory for this budget alongside Home Assistant and other apps.
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
`python tests/codex_verification_smoke.py` uses the pinned CLI and a local mock model
endpoint to check MCP discovery and real entity-tool calls in both sandbox modes,
while confirming that shell socket connections remain denied. Set `HA_TEST_CODEX`
to the pinned CLI binary when it is not at `/usr/local/bin/codex`. No model account
or paid request is involved.
`node tests/browser_verification.cjs` uses real Chromium and a protocol fixture to
check viewports, external auth, registered local resources, blocked writes, broken resources/cards, rejected
authentication, and cleanup. Provide `playwright-core` and `ws` on `NODE_PATH` and
set `HA_BROWSER_EXECUTABLE` when Chromium is not at `/usr/bin/chromium-browser`.
`node tests/browser_resources.cjs` tests static-resource permissions and the
external HTTPS transport, including header stripping, DNS pinning, private
addresses, redirect revalidation, and response limits, without external requests.

Set `HA_BROWSER_NODE=node` to include the real Home Assistant frontend test under
`tests/ha`. CI also builds both container architectures. Fixture tests are not a
deployment test of a user's HAOS kernel, AppArmor policy, or installed custom cards.

References: [app authentication](https://developers.home-assistant.io/docs/apps/communication/),
[external authentication](https://developers.home-assistant.io/docs/frontend/external-authentication/),
and [WebSocket routing](https://playwright.dev/docs/api/class-websocketroute).
