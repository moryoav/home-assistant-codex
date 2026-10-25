# Home Assistant verification

**Codex can inspect your live dashboards in a real browser at desktop and mobile
sizes, review screenshots during a task, and revise its work.** The conversation
shows the screenshots and findings, including missing cards, failed resources,
and browser errors.

Ask: **"Inspect `/lovelace/home` on desktop and mobile, show me screenshots, and
report any layout or card errors."** Replace the path with your dashboard view.
Keep **Enable built-in browser** enabled in the app's **Configuration** tab.

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
Policy blocks limit what this browser can verify; they do not establish faults
on a tablet or other browser where the dashboard is used. A policy block does
not mean the request changes anything. Each blocked WebSocket request explains
why it was refused and whether it can be reviewed for approval. Home Assistant
access errors are reported separately; browser approval cannot change the
permissions of the temporary read-only Home Assistant account.

Built-in `state` and `numeric_state` condition subscriptions, including nested
`and`, `or`, and `not` combinations, are allowed automatically. They read entity
values and report changes to a condition's result. The controller checks the
predicate types and fields and bounds the request's size and nesting; Home
Assistant validates the condition values. Unfamiliar condition types still need
review. At most 64 condition subscriptions can be active across the inspection's
connections at once. A failed subscription, successful unsubscription, or closed
connection releases its slots. This resource limit cannot be raised by approval.
Knob Swipe Navigation's configuration and rotation subscriptions are
also allowed automatically because they only read configuration or listen for
events. Reporting navigation results remains blocked because it updates the
integration's state.

Core versions before 2026.10 also require administrator access for
`subscribe_condition`. The controller allows supported read-only predicates
through, but those versions of Home Assistant can still refuse them. The result
explains the account restriction; an extra browser approval cannot resolve it.

## Approving additional browser requests

After an inspection, open its verification result and choose requests under
**Review blocked browser requests**. Select individual requests or **Select all
requests**, inspect their details, then choose **Allow selected and retry**.
Unselected requests stay blocked. **Keep blocked** closes the result without
granting access. A batch is available only for the current exchange after tasks
in every chat have finished, and can be approved once. Each exchange permits up
to four retries. An inspection offers at most 16 complete requests, and private
storage holds at most 128 remembered rules. Additional requests may be omitted to
keep the evidence bounded; an approvable request is never shortened to fit.

An approval starts a fresh dashboard capture with a new short-lived session.
It does not rerun the task, save dashboard changes, reload Home Assistant, or
change the original answer, and it neither moves the chat in the recent list
nor changes the integration's latest-task sensor. While the retry runs it counts
as worker activity: messages sent meanwhile wait in the queue. Approval choices
and new evidence remain with the exchange. A one-off approval is never saved; it
applies to that retry and to later retries in the same exchange, so requests
that depend on each other can be approved one batch at a time. Active one-off
grants stay in worker memory and expire when the worker restarts. Chat files
retain the approval history, but cannot grant access when restored. Select
**Remember for this dashboard** to reuse the selected permissions in later
captures of that view; a trailing slash in the path doesn't matter. Saved
permissions survive worker and Home Assistant restarts and are bound to the Core
installation identity (the integration's browser user) and Core origin. Moving
to another installation or changing the origin requires new approvals. Accepted
batches are recorded before a capture starts, so an interrupted retry cannot
make an old approval reusable after a restart. Start a new inspection if the
worker stops before it can record the retry result.

Open **Settings > Browser permissions** to inspect and revoke saved exceptions.
Revoking a saved exception stops captures that loaded it; other changes don't.
Permissions are kept in private app storage, outside `/config`, with bounded,
atomic storage. An entry a later version no longer accepts is ignored and listed
as unsupported so you can revoke it. If the file can't be read at all, captures
run without saved permissions and say so, and Settings offers **Remove all saved
permissions**. Permissions cannot be granted or revoked through the integration
API token or the model's verification tool; management requires the human Home
Assistant Ingress UI. Exact request details appear only there: the model's
verification tool, task-result events and token-authenticated task API calls
get each request's label. Update both the worker and integration for approval
support. With an older integration, screenshots still work and the result asks
you to update the integration before requests can be approved.

User approval is a trust decision, not certification that an unfamiliar custom
handler has no side effects. Generic exceptions cover bounded read-like WebSocket
operations with their exact parameters, or exact same-origin GET/HEAD resources
and query parameters. Changing the request parameters requires another approval.
Repeated query parameters retain their value order, so changing which value
comes first requires another approval.
Request IDs and object-key order do not change a WebSocket permission. Requests
with hidden credentials, malformed query encoding, oversized details, or
unsupported actions cannot be approved. Known device controls, writes (also when
the write word is separated by `-` or `.` or written in camelCase), sign-in
changes, URL signing, camera WebRTC setup/signaling, redirects and arbitrary
external access stay blocked. Commands the controller restricts by name or
parameters (Knob Swipe Navigation, Custom Icons, Browser Mod and event
subscriptions) cannot be widened by an approval. `browser_permission_policy.json`
holds these rules for both the controller and the worker.

Browser Mod remains separate. Its `connect` handler can update stored browser
details and attach a registered browser to its command channel, so it needs
dedicated support rather than a generic approval. A `zha_event` subscription is
read-only, but Home Assistant restricts that event stream to administrators.
The inspection browser keeps its read-only account, so this subscription is
unavailable even with user approval. Listening through the supported knob
rotation subscription does not require access to that administrator event stream.

**Load raster map tiles** is one bounded permission for the map-only token and
valid raster tile coordinates, rather than a permission per image URL. The real
map token remains in the controller; page scripts receive a placeholder. The
controller opens Core's WebSocket itself, so page scripts never hold the real
connection, access token or map token. Signing URLs and camera WebRTC
setup/signaling require separate support. These permissions do not observe a
physical WallPanel or reproduce its app transitions.

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

Update both the worker and integration to **0.1.62**, followed by a Home
Assistant restart, to discover custom integrations' static routes, including
individual files on Core 2026.9.4. Older brokers
still support the original built-in paths but cannot supply the new route list.

Core supplies registered static file and directory URLs, never filesystem paths.
This allows assets such as Browser Mod, custom icons, navigation modules, and
WebRTC card scripts without granting access to arbitrary integration GET endpoints.
Loading a script does not permit its service calls, writes, or unsupported APIs.
The read-only `recorder/statistic_during_period` and `camera/capabilities`
commands are supported.
Custom Icons' active-set, list, cache, and single-icon reads are supported with
bounded set/icon names; icon selection and download operations remain blocked.
Knob Swipe Navigation's configuration read is supported. Its live knob-event
subscriptions and navigation-result writes remain blocked, as do Browser Mod
connection/registration requests. Features depending on them may still report
blocked requests.

Same-origin scripts and stylesheets explicitly registered as Core frontend modules
or Lovelace resources can also load through custom HTTP views. Only the exact
registered URL, including its query string, is allowed; registration does not
grant access to sibling paths or other query strings. Assets referenced by
same-origin CSS can load at their exact URLs too. These permissions allow only
GET/HEAD asset requests outside `/api` and `/auth`; they do not enable navigation or writes.

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
  messages use an explicit read allowlist, plus exceptions the owner approves.
  Request IDs must increase on each connection, as Core requires. Service calls,
  saves, arbitrary event subscriptions, external navigation, and service workers
  are blocked. External
  assets use the separate restricted transport described above. Unsupported
  custom-card requests are reported instead of silently expanding access.
- The dedicated identity may see different cards, themes, or views from a household
  member. Administrator-only dashboards, unsupported external resources, and cards requiring
  writes during initialization may not be available.
- At most 24 checks and four browser runs per turn, plus up to four approved
  retries, one at a time. Each browser run
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
but are excluded from per-file backups and full snapshots. Snapshots made before
0.1.58 are not rewritten; like all backups they are deleted after the retention
period. Those files need Home Assistant backups for recovery. Other files can contain inline
secrets, so this is targeted protection, not complete secret detection.

Chromium uses the app's container/AppArmor confinement with its own sandbox disabled
because the app runs as root. It receives no Supervisor or worker credentials in its
environment. The worker, CLI, and browser share the container and filesystem; these
controls are not isolation against a compromised root process. Do not describe them
as complete credential isolation or a hardened browser sandbox.

## Validation

Live browser verification has been confirmed working by the maintainer on their
Home Assistant installation. Custom-card compatibility remains subject to the
resource and permission limits above.

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
`node tests/browser_permissions.cjs` tests approval eligibility, exact matching,
request-ID correlation and bounded evidence without a browser, and checks that
the worker's Python validator agrees with the controller on the same requests.

Set `HA_BROWSER_NODE=node` to include the real Home Assistant frontend test under
`tests/ha`. CI runs broker discovery and the real frontend capture on Core
2026.6.1 and 2026.9.4, covering both HTTP static-file implementations. The browser
uses the broker's discovered resources rather than a hand-written resource list.
CI also builds both container architectures. Fixture tests are not a
deployment test of a user's HAOS kernel, AppArmor policy, or installed custom cards.

References: [app authentication](https://developers.home-assistant.io/docs/apps/communication/),
[external authentication](https://developers.home-assistant.io/docs/frontend/external-authentication/),
and [WebSocket routing](https://playwright.dev/docs/api/class-websocketroute).
