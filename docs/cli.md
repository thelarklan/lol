# LOL CLI reference

LOL commands operate on the Git repository containing the current directory. Commands that need
project configuration load `lol.yaml`, the user configuration at
`${XDG_CONFIG_HOME:-~/.config}/lol/config.yaml`, and explicit command options in that order.

Use `lol --format json <command>` for machine-readable output where supported. JSON mode never
opens an interactive prompt. `lol --version` prints the installed version.

## Repository setup

### `lol init`

Discovers Jenkinsfiles, node labels, Podman use, and host commands, then previews and creates
`lol.yaml` plus `lol.plugins.lock.yaml`.

Important options:

- `--jenkinsfile PATH`, `--jenkins-version VERSION`, repeated `--label LABEL`, and
  `--executors 1..32` provide explicit contract values.
- `--detected-labels` or `--no-detected-labels` resolves discovered labels.
- Repeated `--require COMMAND` and `--require-podman` declare host requirements.
- `--non-interactive` disables prompts; unresolved choices fail with exit `2`.
- `--yes` accepts recommended LOL-owned choices. It does not provide secrets or approve privileged
  host changes.
- `--force` permits replacement of existing configuration after the normal diff preview.

### `lol config show` and `lol config edit`

`config show` prints the effective configuration with secret-shaped values redacted. `config edit`
reuses guided discovery and preserves repository-owned pipeline parameters, environment settings,
plugins, and artifact patterns. `config edit --yes` accepts the displayed update.

### `lol lock`

Resolves the complete plugin graph for the repository-owned Jenkins contract and writes exact URLs,
versions, and SHA-256 hashes. `--yes` accepts the preview. `lol lock --check` is read-only and fails
when the committed lock is missing, invalid, or stale.

### `lol doctor`

Checks Git, Java, the committed lock and cache, declared commands, ports, and rootless Podman when
required.

- `--check` performs read-only analysis.
- `--fix` offers discovered LOL-owned repairs.
- `--yes` applies safe LOL-owned repairs without prompting.
- `--verbose` includes diagnostic evidence.

## Controller lifecycle

- `lol up [--timeout SECONDS]` creates or starts the pinned loopback-only controller.
- `lol status` reports controller state and the latest recorded run.
- `lol open` opens the loopback Jenkins URL in the default browser.
- `lol down [--timeout SECONDS] [--yes]` stops Jenkins while preserving generated state and run
  history. An active run requires confirmation or `--yes`.
- `lol reset [--yes]` previews and removes generated controller/runtime state, preserves run history,
  and recreates the controller. Noninteractive use requires `--yes`.

## Pipeline lifecycle

### `lol run`

Creates an immutable run-scoped snapshot, starts Jenkins when necessary, reconciles the Pipeline
job, streams a redacted console, downloads selected artifacts, and records the result.

```bash
lol run [--revision REVISION] [--jenkinsfile PATH] \
  [--parameter KEY=VALUE]... \
  [--secret-parameter KEY=prompt|stdin|env:NAME]... \
  [--credential ID=SPECIFICATION]... \
  [--trust-repository] [--non-interactive]
```

`--revision` runs a committed revision; without it, tracked changes and non-ignored untracked files
are included. Only one `lol run` may own a project's generated job at a time.

Repositories are untrusted until the user confirms that their Jenkinsfile may execute with the
user's host privileges. `--trust-repository` records that explicit decision. Ordinary `--yes` does
not grant repository trust.

Temporary credentials support:

- `ID=secret-text:prompt|stdin|env:NAME`
- `ID=username-password:prompt`
- `ID=username-password:env:USER_VARIABLE,PASSWORD_VARIABLE`

Secret values must not be literal command arguments. LOL stores only parameter names and credential
IDs, redacts values before displaying or persisting console output, and deletes temporary Jenkins
credentials during cleanup.

`--controller-timeout` controls startup waiting and `--queue-timeout` controls scheduling waiting.

### Run inspection and control

- `lol runs` lists durable run records newest first.
- `lol logs [--run ID] [--follow]` reads the latest or selected redacted console. JSON output and
  `--follow` cannot be combined.
- `lol stop [--run ID] [--yes]` cancels a queued or running build. Cancellation always requires
  confirmation unless `--yes` is supplied.
- `lol artifacts [--run ID]` lists the artifact index.
- `lol artifacts [--run ID] --output DIRECTORY` copies already-downloaded artifacts while preserving
  safe relative paths.

## Exit codes

| Code | Meaning |
| ---: | --- |
| `0` | Command succeeded, or Jenkins returned `SUCCESS` |
| `1` | Jenkins completed with a non-success result such as `FAILURE`, `UNSTABLE`, or `ABORTED` |
| `2` | Usage, configuration, or a required interaction decision is invalid or missing |
| `3` | Doctor found a blocked or unsupported host |
| `4` | Controller provisioning, Jenkins communication, or another harness operation failed |
| `130` | Command was interrupted |

Pipeline exit `1` is distinct from harness exit `4`, so automation can tell a tested Jenkinsfile
failure from a failure to run Jenkins.
