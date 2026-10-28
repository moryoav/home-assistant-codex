# Contributing to Codex for Home Assistant

Thank you for your interest in improving Codex for Home Assistant. I welcome bug reports, documentation updates, and code contributions.

This project has two main parts:

- `codex-cli-worker`: the Home Assistant app/add-on that runs Codex CLI tasks against `/config`.
- `custom_components/codex_cli`: the Home Assistant custom integration that exposes entities, actions, diagnostics, and setup flow support.

Contributions are welcome, including bug reports, documentation improvements, compatibility fixes, security hardening, and feature ideas.

## Before You Start

Please open an issue before starting large or risky changes. This helps avoid duplicated work and gives maintainers a chance to discuss the approach first.

Small fixes, documentation updates, and clearly scoped bug fixes can usually go straight to a pull request.

## Reporting Bugs

When reporting a bug, please include:

- Both the worker app and Codex integration versions.
- Your Home Assistant version.
- Whether you installed through HACS, manually, or from the development branch.
- Your architecture, such as `amd64` or `aarch64`.
- Clear steps to reproduce the issue.
- Relevant logs from Home Assistant or the Codex CLI Worker app.
- What you expected to happen.
- What actually happened.

Please remove secrets, tokens, URLs, device codes, personal paths, and private Home Assistant configuration before sharing logs or screenshots.

## Suggesting Features

Feature requests are welcome. Please describe:

- The problem you want to solve.
- The workflow you expect to use in Home Assistant.
- Whether the change belongs in the worker app, the custom integration, or both.
- Any security or privacy concerns the feature may introduce.

Because this project can modify a Home Assistant configuration folder, features that expand write access, automation behavior, authentication, or remote control should include a clear safety rationale.

## Development Setup

Clone the repository:

```bash
git clone https://github.com/moryoav/home-assistant-codex.git
cd home-assistant-codex
```

The repository layout is:

```text
codex-cli-worker/              Home Assistant app/add-on worker
custom_components/codex_cli/   Home Assistant custom integration
examples/                      Example Home Assistant scripts
.github/workflows/             GitHub Actions validation, test, build, and release workflows
```

For local Home Assistant testing, install or copy the integration into:

```text
/config/custom_components/codex_cli
```

For worker app testing, add this repository as a Home Assistant app repository. To use a development branch, append `#branch-name` to the repository URL. That branch selection applies only to the worker app; install integration changes from the branch manually.

## Pull Request Guidelines

Please keep pull requests focused. A good pull request should:

- Explain what changed and why.
- Mention any related issue.
- Keep unrelated formatting or refactoring out of the change.
- Update documentation when behavior, installation, options, actions, or entities change.
- Include screenshots when changing Home Assistant UI or notifications.
- Avoid committing secrets, credentials, device codes, task logs, or private Home Assistant configuration.

If you change the worker app version, update the relevant version fields consistently.

## Testing

Before opening a pull request, test the parts you changed as much as practical.

Run the full Python suite from the repository root:

```sh
pytest
```

The Home Assistant tests need Linux and Python 3.14. Use WSL on Windows because Home Assistant imports Unix-only modules such as `fcntl`. The [verification workflow](.github/workflows/verification.yaml) pins compatible Home Assistant Core and `pytest-homeassistant-custom-component` versions and lists the remaining Python and browser dependencies. Use a matching pair from that matrix when preparing a test environment. The script `codex-cli-worker/tests/install_frontend_test_deps.py` installs the extra dependencies used by the frontend tests.

For the worker's standalone Python tests, without the Home Assistant pytest fixtures, run:

```sh
python -m unittest discover -s codex-cli-worker/tests -v
```

For a local chat UI check, start `python codex-cli-worker/tests/web_fixture.py`, redirecting its output to a file, then run `node codex-cli-worker/tests/browser_smoke.cjs` in another terminal with Playwright on `NODE_PATH`. Set `CODEX_CHAT_BROWSER=msedge` to use installed Edge when Playwright's Chromium is unavailable. The fixture uses temporary data and never contacts Home Assistant or starts Codex. Stop it after testing and before starting another instance on port 9137. See [verification checks](codex-cli-worker/VERIFICATION.md#validation) and [sandbox checks](codex-cli-worker/SANDBOX_TESTING.md) for those features.

For integration changes, verify that Home Assistant can:

- Load the `codex_cli` integration.
- Complete the config flow.
- Create the expected entities.
- Call the relevant actions.
- Reload or restart without errors.

For worker app changes, verify that the app can:

- Start successfully.
- Serve the Ingress UI.
- Report status.
- Start and track a task.
- Handle login, logout, cancellation, and task input flows when relevant.

For documentation-only changes, please check that links, paths, and examples are accurate.

Automated tests do not cover every installed dashboard, device, or HAOS kernel. Include clear manual test notes for behavior the suite does not exercise.

## Security Notes

This project is powerful by design. It gives Codex access to the Home Assistant configuration folder so it can inspect and modify dashboards, automations, scripts, integrations, and related files.

Please be especially careful with changes involving:

- Authentication or token handling.
- Home Assistant Supervisor or Core API calls.
- Ingress validation.
- File writes under `/config`.
- Task execution, cancellation, and user replies.
- AppArmor, container permissions, networking, or mounted paths.

Do not include real credentials, API tokens, device codes, private logs, or personal Home Assistant configuration in issues or pull requests.

If you believe you found a security vulnerability, please do not open a public issue with exploit details. Follow the reporting process in [SECURITY.md](SECURITY.md).

## Documentation

Please update documentation when changing user-facing behavior. Depending on the change, this may include:

- `README.md`
- `custom_components/codex_cli/README.md`
- `codex-cli-worker/README.md`
- `codex-cli-worker/DOCS.md`
- `examples/scripts.yaml`

Use plain, direct language and include Home Assistant examples where they make the workflow easier to understand.

## Releases

Container images for the worker app are built through GitHub Actions and published to GitHub Container Registry.

Stable users should use the default repository URL:

```text
https://github.com/moryoav/home-assistant-codex
```

For optional worker development and canary testing, use the `dev` branch:

```text
https://github.com/moryoav/home-assistant-codex#dev
```

To prepare a release, follow [Publishing Images](README.md#publishing-images). The release workflow checks version alignment, attaches the HACS ZIP, and publishes worker images. Ordinary pushes to `main` run validation and tests without creating a release or publishing images. Documentation-only changes do not need a version bump.

## Code of Conduct

Please be respectful, constructive, and patient. This project is intended to help Home Assistant users work more safely and effectively with their own configurations, and contributions should support that goal.
