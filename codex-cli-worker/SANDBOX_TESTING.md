# Sandbox compatibility workaround and verification

This worker includes a temporary workaround for [upstream Codex issue #44329](https://github.com/openai/codex/issues/44329), reported here as [issue #9](https://github.com/moryoav/home-assistant-codex/issues/9).

## Scope

The `codex-bwrap` wrapper recognizes Codex's internal, harmless `/bin/true` or `/usr/bin/true` proc-mount probe by its namespace flags and exact command after `--`. Only that probe uses `codex-bwrap-probe.py`. On failure, the helper changes complete, recognized `Can't mount proc on /proc` diagnostic lines to `Can't mount proc on /newroot/proc`, preserving the original failure status. This lets Codex select its own no-proc fallback.

No Bubblewrap arguments are removed, permissions are not expanded, and normal task commands continue through the original direct `exec` path with `--cap-drop ALL`. The CLI version, model choices, integration, AppArmor policy, and sandbox defaults are unchanged. There is no automatic downgrade to `danger-full-access`.

The no-proc fallback is not identical to mounting a fresh `/proc`: it retains the other sandbox restrictions but can expose some process metadata from the app container. This workaround does not add host PID access.

## Readiness diagnostics

The worker now verifies a real Codex sandbox execution in addition to the raw Bubblewrap namespace check. It uses the selected `read-only` or `workspace-write` mode, `/config` working directory, and worker environment. The command is `/bin/true`, not an AI task. Errors and timeouts leave readiness false and block task launch; login and diagnostics remain accessible.

The execution probe has its own 20-second timeout (`CODEX_SANDBOX_PROBE_TIMEOUT_SECONDS`), separate from the 5-second raw Bubblewrap probes. It starts Node, the native CLI, and several Bubblewrap processes, so slow or busy hardware needs more time, and a timeout would otherwise block every task.

In `/health`, `sandbox_readiness.namespace_probe.codex_probe` reports the actual execution result when the raw namespace check succeeded. The standalone `proc_probe` is still included, even when Codex fails. A failed standalone proc mount with successful Codex execution is allowed, but the log no longer promises that a fallback was used merely because a raw namespace test passed.

`danger-full-access` keeps its existing behavior and skips sandbox probes.

## Automated tests

Run from the repository root:

```sh
python -m unittest discover -s codex-cli-worker/tests -v
```

The fake-Bubblewrap tests cover both true-command paths, recognized error reasons and line endings, unchanged unrelated errors, successful probes, capability dropping, preserved descriptors and signal termination, direct-exec PID identity, help/version calls, and task arguments that must not be mistaken for the internal probe. Readiness tests cover both sandbox modes, real-probe failures despite raw namespace success, timeouts, missing CLI, and unrestricted-mode bypass.

These tests do not prove HAOS/AppArmor compatibility or filesystem isolation on the affected host.

## HAOS verification before release

Build/install this branch as a test worker image; keep the existing protection and AppArmor settings. On the affected HAOS installation, verify both `read-only` and `workspace-write`, not just `danger-full-access`.

From a shell **inside the worker container**, the basic execution checks are:

```sh
export CODEX_HOME=/data/codex-home HOME=/data
cd /config
/usr/local/bin/codex sandbox linux --config 'sandbox_mode="read-only"' -- /bin/true
/usr/local/bin/codex sandbox linux --config 'sandbox_mode="workspace-write"' -- /bin/true
```

Both should exit successfully. Then run the original issue's read-only inspection task and confirm actual shell execution, not just an assistant reply. In a disposable test setup, use new temporary files to verify that `read-only` denies writes, `workspace-write` allows a test write under `/config`, and writes outside configured writable roots (for example a new test file under `/data`) are still denied. Do not use existing HA configuration or credential files for write tests. Also check normal task cancellation.

Repeat the basic checks on a host where fresh `/proc` mounting succeeds. A successful `/bin/true` check alone is not proof that all write restrictions are enforced.

## Removing the workaround

Once the pinned upstream CLI recognizes both proc error formats, remove the special-probe branch/helper and its Docker copy. Retain the real Codex readiness check and fail-closed tests. Verify the affected HAOS setup before removing compatibility code or declaring issue #9 resolved.
