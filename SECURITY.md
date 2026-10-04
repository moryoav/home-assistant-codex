# Security Policy

Codex for Home Assistant can read and modify a Home Assistant configuration folder. Please treat security and privacy issues with care.

Dashboard verification uses a dedicated local identity, temporary credentials,
restricted HTTP/WebSocket requests, and private screenshot attachments. The browser
shares the app's container and filesystem and runs without Chromium's own sandbox;
this is not complete isolation from the AI process or root access. Known credential
files are excluded from backups and snapshots, but inline secrets and old snapshots
need separate care. See [verification security and retention](codex-cli-worker/VERIFICATION.md).

Dashboard rendering can fetch supported public HTTPS scripts, styles, fonts, and
images through a separate client without Home Assistant credentials or browser
cookies. The client validates and pins public DNS addresses and rechecks redirects.
External servers still see the host's public IP and resource URL. Installed custom
cards remain trusted code with access to dashboard data. Service calls and writes
stay blocked during verification. See [resource policy and limits](codex-cli-worker/VERIFICATION.md#dashboard-resources).

## Supported Versions

Security fixes are intended for the latest published release and the current `main` branch.

Development and canary testing may happen on the `dev` branch. Older releases are not actively supported unless a maintainer says otherwise in a specific issue or release note.

## Reporting a Vulnerability

Please do not open a public issue with exploit details, working proof-of-concept code, private logs, tokens, device codes, or personal Home Assistant configuration.

If GitHub private vulnerability reporting is available for this repository, use the **Report a vulnerability** button on the Security tab.

If private vulnerability reporting is not available, open a minimal public issue that says you have a security concern and asks the maintainer to arrange private disclosure. Do not include sensitive details in that issue.

## What to Include

When reporting a vulnerability privately, include as much of the following as you can safely share:

- A clear description of the issue.
- The affected version or commit.
- Whether the issue affects the worker app, the custom integration, or both.
- Steps to reproduce in a safe test environment.
- The expected impact.
- Any relevant logs with secrets and private configuration removed.
- Suggested mitigations, if you know them.

## Security-Sensitive Areas

Please use extra care when changing or reviewing:

- Codex CLI authentication and logout behavior.
- Worker API token generation, storage, rotation, or validation.
- Home Assistant Ingress validation.
- Home Assistant Supervisor or Core API calls.
- File reads and writes under `/config`.
- Task execution, cancellation, user replies, and task logs.
- AppArmor, container permissions, networking, mounted paths, and app/add-on configuration.

## Responsible Testing

Test security reports and fixes only in an environment you own or have permission to use. Do not attempt to access, modify, or disclose another person's Home Assistant instance, configuration, credentials, logs, or devices.

## Public Disclosure

Please give the maintainer reasonable time to investigate and fix confirmed vulnerabilities before publishing details publicly. Coordinated disclosure helps protect users while a fix is prepared.
