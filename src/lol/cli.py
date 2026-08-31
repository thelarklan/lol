from __future__ import annotations

import difflib
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import click
import yaml

from lol import __version__
from lol.artifacts import copy_artifacts, load_artifact_index
from lol.config import (
    DEFAULTS,
    EffectiveConfig,
    deep_merge,
    dump_yaml,
    load_effective,
    read_yaml,
    redact,
    validate_manifest,
)
from lol.constants import (
    EXIT_HOST,
    EXIT_INTERRUPTED,
    EXIT_SUCCESS,
    EXIT_USAGE,
    LOCK_NAME,
    MANIFEST_NAME,
    PINNED_JENKINS_VERSION,
)
from lol.controller import ControllerStatus, open_ui
from lol.controller import credentials as controller_credentials
from lol.controller import down as controller_down
from lol.controller import reset as controller_reset
from lol.controller import status as controller_status
from lol.controller import up as controller_up
from lol.discovery import discover, initial_manifest
from lol.doctor import Finding, analyze, final_state
from lol.errors import ConfigError, InteractionError, LolError
from lol.io import write_yaml, write_yaml_bundle
from lol.jenkins import JenkinsClient
from lol.lockfile import create_lock, ensure_lock_cache, load_lock
from lol.paths import ProjectPaths
from lol.project import ProjectIdentity, find_repository, project_identity
from lol.runner import (
    RESERVED_PARAMETERS,
    TemporaryCredential,
    execute,
    load_parameter_definitions,
    read_console,
)
from lol.runs import RunRecord, list_runs, load_run
from lol.trust import is_trusted, trust


class Context:
    def __init__(self, output_format: str) -> None:
        self.output_format = output_format

    def emit(self, value: Any) -> None:
        if self.output_format == "json":
            click.echo(json.dumps(value, indent=2, sort_keys=True))
        elif isinstance(value, str):
            click.echo(value)
        else:
            click.echo(yaml.safe_dump(value, sort_keys=False).rstrip())


def _is_interactive() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def _resolved_project() -> tuple[EffectiveConfig, ProjectIdentity, ProjectPaths]:
    config = load_effective()
    identity = project_identity(config.root, config.values)
    return config, identity, ProjectPaths.from_id(config.root, identity.project_id)


@click.group()
@click.version_option(__version__, prog_name="lol")
@click.option("output_format", "--format", type=click.Choice(["text", "json"]), default="text")
@click.pass_context
def cli(click_context: click.Context, output_format: str) -> None:
    """Run a repository's Jenkinsfile on a reproducible local Jenkins controller."""
    click_context.obj = Context(output_format)


@cli.group("config")
def config_group() -> None:
    """Inspect effective configuration."""


@config_group.command("show")
@click.pass_obj
def config_show(context: Context) -> None:
    """Show resolved non-secret configuration and its sources."""
    config = load_effective()
    context.emit(
        {
            "configuration": redact(config.values),
            "sources": config.sources,
            "lock_inputs": {
                "keys": ["jenkins.version", "jenkins.plugins"],
                "source": config.sources["repository"],
                "note": "Committed locks ignore user and command-line overrides for these keys.",
            },
        }
    )


def _diff(path: Path, content: str) -> str:
    if path.is_symlink():
        raise InteractionError(f"refusing to replace symbolic link: {path}")
    try:
        before = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    except OSError as exc:
        raise InteractionError(f"cannot read {path}: {exc}") from exc
    after = content.splitlines()
    lines = difflib.unified_diff(
        before,
        after,
        fromfile=str(path),
        tofile=str(path),
        lineterm="",
    )
    rendered = "\n".join(lines)
    return f"{rendered}\n" if rendered else ""


def _select_jenkinsfile(
    root: Path,
    found: tuple[Path, ...],
    requested: str | None,
    *,
    interactive: bool,
) -> Path:
    if requested:
        candidate = Path(requested)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ConfigError("--jenkinsfile must be repository-relative without '..'")
        try:
            resolved = (root / candidate).resolve()
            resolved.relative_to(root.resolve())
        except (OSError, RuntimeError, ValueError) as exc:
            raise ConfigError("--jenkinsfile must remain inside the repository") from exc
        if not resolved.is_file():
            raise ConfigError(f"Jenkinsfile not found: {candidate}")
        return candidate
    if len(found) == 1:
        return found[0]
    if not found:
        raise ConfigError("no Jenkinsfile was found; use --jenkinsfile")
    if not interactive:
        choices = ", ".join(path.as_posix() for path in found)
        raise InteractionError(f"multiple Jenkinsfiles found ({choices}); use --jenkinsfile")
    click.echo("Found Jenkinsfiles:")
    for index, path in enumerate(found, 1):
        click.echo(f"  {index}. {path}")
    selected = click.prompt(
        "Which Jenkinsfile should LOL run?",
        type=click.IntRange(1, len(found)),
        default=1,
    )
    return found[selected - 1]


def _selected_labels(
    discovered: tuple[str, ...],
    supplied: tuple[str, ...],
    detected_labels: bool | None,
    *,
    interactive: bool,
    yes: bool,
) -> tuple[str, ...]:
    if detected_labels is True:
        return tuple(sorted({*supplied, *discovered}))
    if detected_labels is False or supplied or not discovered:
        return tuple(sorted(set(supplied)))
    if yes:
        return discovered
    if not interactive:
        raise InteractionError(
            "pipeline labels were detected; use --detected-labels, --no-detected-labels, "
            "--label, or --yes"
        )
    click.echo(f"Detected labels: {', '.join(discovered)}")
    return discovered if click.confirm("Add these labels to the local node?", default=True) else ()


def _selected_podman(
    discovered: bool,
    requested: bool | None,
    *,
    interactive: bool,
    yes: bool,
) -> bool:
    if requested is not None:
        return requested
    if not discovered:
        return False
    if yes:
        return True
    if not interactive:
        raise InteractionError(
            "Podman usage was detected; use --require-podman, --no-require-podman, or --yes"
        )
    return click.confirm("Podman usage detected. Require rootless Podman?", default=True)


def _write_configuration(
    manifest_path: Path,
    manifest: dict[str, Any],
    lock_path: Path,
    lock: dict[str, Any],
    *,
    yes: bool,
    interactive: bool,
) -> bool:
    manifest_preview = _diff(manifest_path, dump_yaml(manifest))
    lock_preview = _diff(lock_path, dump_yaml(lock))
    if not manifest_preview and not lock_preview:
        click.echo("LOL configuration is already up to date.")
        return False
    click.echo(manifest_preview, nl=False)
    click.echo(lock_preview, nl=False)
    if not yes:
        if not interactive:
            raise InteractionError("confirmation is unavailable; review the diff and use --yes")
        if not click.confirm("Write lol.yaml and lol.plugins.lock.yaml?", default=True):
            raise InteractionError("configuration update cancelled")
    try:
        write_yaml_bundle(((lock_path, lock), (manifest_path, manifest)))
    except OSError as exc:
        raise InteractionError(f"cannot write LOL configuration: {exc}") from exc
    return True


def _init_impl(
    *,
    jenkinsfile: str | None,
    jenkins_version: str | None,
    labels: tuple[str, ...],
    detected_labels: bool | None,
    requirements: tuple[str, ...],
    require_podman: bool | None,
    executors: int | None,
    non_interactive: bool,
    yes: bool,
    force: bool,
    base: dict[str, Any] | None = None,
) -> None:
    root = find_repository()
    manifest_path = root / MANIFEST_NAME
    lock_path = root / LOCK_NAME
    if (manifest_path.exists() or lock_path.exists()) and not force:
        raise InteractionError("LOL configuration already exists; use --force to replace it")
    interactive = not non_interactive and _is_interactive()
    found = discover(root)
    click.echo(f"Found repository: {root.name}")
    selected = _select_jenkinsfile(
        root,
        found.jenkinsfiles,
        jenkinsfile,
        interactive=interactive,
    )
    selected_version = jenkins_version
    if selected_version is None:
        selected_version = (
            click.prompt("Jenkins version", default="pinned-lts")
            if interactive and not yes
            else "pinned-lts"
        )
    effective_labels = _selected_labels(
        found.labels,
        labels,
        detected_labels,
        interactive=interactive,
        yes=yes,
    )
    podman = _selected_podman(
        found.podman,
        require_podman,
        interactive=interactive,
        yes=yes,
    )
    selected_executors = executors
    if selected_executors is None:
        selected_executors = (
            click.prompt(
                "Maximum simultaneous Jenkins builds",
                type=click.IntRange(1, 32),
                default=1,
            )
            if interactive and not yes
            else 1
        )
    generated = initial_manifest(
        selected,
        jenkins_version=selected_version,
        labels=(
            effective_labels if base is not None else tuple(sorted({"linux", *effective_labels}))
        ),
        require_podman=podman,
        executors=selected_executors,
        commands=requirements,
    )
    if base is None:
        manifest = generated
    else:
        manifest = deep_merge(
            base,
            {
                "jenkins": {"version": selected_version},
                "pipeline": {"file": generated["pipeline"]["file"]},
                "node": generated["node"],
                "requirements": generated["requirements"],
            },
        )
    validate_manifest(manifest)
    effective_values = deep_merge(DEFAULTS, manifest)
    if effective_values["jenkins"]["version"] in {"lts", "pinned-lts"}:
        effective_values["jenkins"]["version"] = PINNED_JENKINS_VERSION
    validate_manifest(effective_values)
    with tempfile.TemporaryDirectory(prefix="lol-init-") as temporary:
        candidate_manifest = Path(temporary) / MANIFEST_NAME
        candidate_lock = Path(temporary) / LOCK_NAME
        write_yaml(candidate_manifest, manifest)
        effective = EffectiveConfig(
            root=root,
            manifest_path=candidate_manifest,
            values=effective_values,
            sources={"built_in": "LOL defaults", "repository": str(candidate_manifest)},
        )
        lock = create_lock(effective, candidate_lock)
        changed = _write_configuration(
            manifest_path,
            manifest,
            lock_path,
            lock,
            yes=yes or (non_interactive and base is None),
            interactive=interactive,
        )
    if changed:
        click.echo(f"Wrote {manifest_path} and {lock_path}")


@cli.command("init")
@click.option("--jenkinsfile", type=click.Path(path_type=Path))
@click.option("--jenkins-version")
@click.option("--label", "labels", multiple=True)
@click.option(
    "--detected-labels/--no-detected-labels",
    default=None,
    help="Ignore discovered labels with --no-detected-labels; defaults remain.",
)
@click.option("--require", "requirements", multiple=True)
@click.option("--require-podman/--no-require-podman", default=None)
@click.option("--executors", type=click.IntRange(1, 32))
@click.option("--non-interactive", is_flag=True)
@click.option("--yes", is_flag=True)
@click.option("--force", is_flag=True)
def init_command(
    jenkinsfile: Path | None,
    jenkins_version: str | None,
    labels: tuple[str, ...],
    detected_labels: bool | None,
    requirements: tuple[str, ...],
    require_podman: bool | None,
    executors: int | None,
    non_interactive: bool,
    yes: bool,
    force: bool,
) -> None:
    """Discover and create lol.yaml plus the exact plugin lock."""
    podman_requirement = "podman" in requirements
    if podman_requirement and require_podman is False:
        raise ConfigError("--require podman conflicts with --no-require-podman")
    if podman_requirement:
        requirements = tuple(item for item in requirements if item != "podman")
        require_podman = True
    _init_impl(
        jenkinsfile=jenkinsfile.as_posix() if jenkinsfile else None,
        jenkins_version=jenkins_version,
        labels=labels,
        detected_labels=detected_labels,
        requirements=requirements,
        require_podman=require_podman,
        executors=executors,
        non_interactive=non_interactive,
        yes=yes,
        force=force,
    )


@config_group.command("edit")
@click.option("--yes", is_flag=True)
def config_edit(yes: bool) -> None:
    """Regenerate repository configuration using guided choices."""
    config = load_effective()
    repository = read_yaml(config.manifest_path)
    repository_values = deep_merge(DEFAULTS, repository)
    labels = tuple(str(item) for item in repository_values["node"]["labels"])
    executors = int(repository_values["node"]["executors"])
    require_podman = bool(repository_values["requirements"]["podman"])
    jenkinsfile = str(repository_values["pipeline"]["file"])
    jenkins_version = str(repository_values["jenkins"]["version"])
    interactive = _is_interactive()
    if interactive and not yes:
        jenkinsfile = click.prompt("Jenkinsfile", default=jenkinsfile)
        jenkins_version = click.prompt("Jenkins version", default=jenkins_version)
        label_text = click.prompt("Node labels (comma-separated)", default=",".join(labels))
        labels = tuple(item.strip() for item in label_text.split(",") if item.strip())
        require_podman = click.confirm("Require rootless Podman?", default=require_podman)
        executors = click.prompt(
            "Maximum simultaneous Jenkins builds",
            type=click.IntRange(1, 32),
            default=executors,
        )
    _init_impl(
        jenkinsfile=jenkinsfile,
        jenkins_version=jenkins_version,
        labels=labels,
        detected_labels=False,
        requirements=tuple(str(item) for item in repository_values["requirements"]["commands"]),
        require_podman=require_podman,
        executors=executors,
        non_interactive=not interactive,
        yes=yes,
        force=True,
        base=repository,
    )


@cli.command("lock")
@click.option("--check", "check_only", is_flag=True, help="Report lock drift without writing.")
@click.option("--yes", is_flag=True, help="Write the displayed lock without confirmation.")
def lock_command(check_only: bool, yes: bool) -> None:
    """Resolve and pin the exact Jenkins plugin graph."""
    config = load_effective()
    path = config.root / LOCK_NAME
    if check_only:
        load_lock(config)
        click.echo("Plugin lock matches lol.yaml.")
        return
    with tempfile.TemporaryDirectory(prefix="lol-lock-") as temporary:
        candidate = Path(temporary) / LOCK_NAME
        lock = create_lock(config, candidate)
        content = candidate.read_text(encoding="utf-8")
        preview = _diff(path, content)
        if not preview:
            click.echo("Plugin lock is already up to date.")
            return
        click.echo(preview, nl=False)
        if not yes:
            if not _is_interactive():
                raise InteractionError("lock update requires an interactive terminal or --yes")
            if not click.confirm("Write the plugin lock?", default=True):
                raise InteractionError("lock update cancelled")
        write_yaml(path, lock)
    click.echo(f"Wrote {path}")


def _render_doctor(findings: list[Finding], *, verbose: bool) -> None:
    icons = {
        "pass": "✓",
        "recommendation": "!",
        "warning": "!",
        "blocker": "✗",
        "unsupported": "✗",
    }
    for finding in findings:
        click.echo(f"{icons[finding.status]} {finding.summary}")
        if verbose and finding.evidence:
            click.echo(f"  {finding.evidence}")
        if finding.recommendation:
            click.echo(f"  {finding.recommendation}")


def _lock_repair(findings: list[Finding]) -> Finding | None:
    return next(
        (
            finding
            for finding in findings
            if finding.check == "plugins.lock"
            and finding.status in {"blocker", "recommendation"}
            and finding.repair_scope == "lol-owned"
        ),
        None,
    )


@cli.command("doctor")
@click.option("--check", "check_only", is_flag=True, help="Analyze without offering repairs.")
@click.option("--fix", is_flag=True, help="Offer repairs for discovered LOL-owned findings.")
@click.option("--yes", is_flag=True, help="Apply safe LOL-owned repairs without prompting.")
@click.option("--verbose", is_flag=True, help="Include diagnostic evidence.")
@click.pass_obj
def doctor_command(context: Context, check_only: bool, fix: bool, yes: bool, verbose: bool) -> None:
    """Validate host prerequisites and pinned inputs."""
    if check_only and (fix or yes):
        raise ConfigError("--check cannot be combined with --fix or --yes")
    config, identity, paths = _resolved_project()
    findings = analyze(config, paths.state)
    repaired = False
    if context.output_format != "json":
        _render_doctor(findings, verbose=verbose)
    repair = _lock_repair(findings)
    if repair is not None and not check_only:
        if context.output_format == "json" and not yes:
            if fix:
                raise InteractionError("JSON doctor repairs require --yes")
            approved = False
        elif yes:
            approved = True
        elif not _is_interactive():
            raise InteractionError("doctor repair requires an interactive terminal or --yes")
        else:
            action = (
                "Download the artifacts named by the plugin lock?"
                if repair.status == "recommendation"
                else "Regenerate and cache the plugin lock?"
            )
            approved = click.confirm(action, default=True)
        if approved:
            if repair.status == "recommendation":
                ensure_lock_cache(load_lock(config))
            else:
                create_lock(config)
            findings = analyze(config, paths.state)
            repaired = True
    state = final_state(findings)
    if context.output_format == "json":
        context.emit(
            {
                "project_id": identity.project_id,
                "state": state,
                "findings": [finding.as_dict() for finding in findings],
            }
        )
    else:
        if repaired:
            click.echo("Revalidated:")
            _render_doctor(findings, verbose=verbose)
        click.echo(f"Doctor result: {state}")
    if state in {"Blocked", "Unsupported"}:
        raise LolError(f"host is {state.lower()}", EXIT_HOST)


def _controller_payload(identity: ProjectIdentity, state: ControllerStatus) -> dict[str, object]:
    value = state.as_dict()
    value["project_id"] = identity.project_id
    return value


@cli.command("up")
@click.option(
    "--timeout",
    type=click.FloatRange(min=0.1),
    default=120.0,
    show_default=True,
    help="Seconds to wait for Jenkins startup.",
)
@click.pass_obj
def up_command(context: Context, timeout: float) -> None:
    """Create or start the repository's local Jenkins controller."""
    config, identity, paths = _resolved_project()
    context.emit(_controller_payload(identity, controller_up(config, paths, timeout=timeout)))


@cli.command("status")
@click.pass_obj
def status_command(context: Context) -> None:
    """Show the repository's local controller and latest run status."""
    _, identity, paths = _resolved_project()
    current = controller_status(paths)
    payload = _controller_payload(identity, current)
    records = list_runs(paths)
    latest: dict[str, Any] | None = None
    if records:
        latest = dict(records[0].metadata)
        if latest.get("status") in {"provisioning", "queued", "running", "stopping"} and (
            current.state != "running"
        ):
            latest["status"] = "stale"
    payload["latest_run"] = latest
    context.emit(payload)


@cli.command("open")
@click.pass_obj
def open_command(context: Context) -> None:
    """Open the repository's local Jenkins controller in a browser."""
    _, identity, paths = _resolved_project()
    endpoint = open_ui(paths)
    context.emit({"project_id": identity.project_id, "endpoint": endpoint})


@cli.command("down")
@click.option(
    "--timeout",
    type=click.FloatRange(min=0.1),
    default=30.0,
    show_default=True,
    help="Seconds to wait for Jenkins shutdown.",
)
@click.option("--yes", is_flag=True, help="Stop without prompting when a run is active.")
@click.pass_obj
def down_command(context: Context, timeout: float, yes: bool) -> None:
    """Stop Jenkins while preserving the repository's generated state."""
    _, identity, paths = _resolved_project()
    active = _active_runs(paths)
    if active and not yes:
        if context.output_format == "json" or not _is_interactive():
            raise InteractionError("a pipeline is active; inspect it and use --yes to stop Jenkins")
        if not click.confirm("A pipeline is active. Stop Jenkins?", default=False):
            raise InteractionError("shutdown cancelled")
    context.emit(_controller_payload(identity, controller_down(paths, timeout=timeout)))


def _active_runs(paths: ProjectPaths) -> list[RunRecord]:
    if controller_status(paths).state != "running":
        return []
    return [
        record
        for record in list_runs(paths)
        if record.metadata.get("status") in {"provisioning", "queued", "running", "stopping"}
    ]


def _select_record(
    paths: ProjectPaths,
    run_id: str | None,
    *,
    active: bool = False,
    latest: bool = False,
    allow_prompt: bool = True,
) -> RunRecord:
    if run_id:
        record = load_run(paths, run_id)
        if active and record.run_id not in {item.run_id for item in _active_runs(paths)}:
            raise ConfigError(f"run is not active: {record.run_id}")
        return record
    records = _active_runs(paths) if active else list_runs(paths)
    if not records:
        raise ConfigError("no matching LOL runs are recorded")
    if len(records) == 1 or latest:
        return records[0]
    if not allow_prompt or not _is_interactive():
        raise InteractionError("multiple runs match; use --run <id>")
    click.echo("Select a run:")
    for index, record in enumerate(records, 1):
        click.echo(
            f"  {index}. {record.run_id} "
            f"({record.metadata.get('status', '-')}/{record.metadata.get('result', '-')})"
        )
    selected = click.prompt("Run", type=click.IntRange(1, len(records)), default=1)
    return records[selected - 1]


def _parse_pair(value: str, option: str) -> tuple[str, str]:
    if "=" not in value:
        raise ConfigError(f"{option} requires KEY=VALUE")
    key, result = value.split("=", 1)
    if not key or key.strip() != key or any(ord(character) < 32 for character in key):
        raise ConfigError(f"{option} requires a non-empty key without outer whitespace")
    return key, result


def _secret_source(
    source: str,
    *,
    interactive: bool,
    allow_stdin: bool = True,
    prompt: str = "Secret",
) -> str:
    if source == "prompt":
        if not interactive:
            raise InteractionError("secure prompt is unavailable; use env:NAME or stdin")
        return str(click.prompt(prompt, hide_input=True, confirmation_prompt=False))
    if source == "stdin" and allow_stdin:
        value = sys.stdin.readline()
        if value == "":
            raise InteractionError("secret input ended before a value was read")
        return value.rstrip("\n")
    if source.startswith("env:"):
        name = source[4:]
        if not name or name not in os.environ:
            raise InteractionError(f"secret environment variable is unavailable: {name}")
        return os.environ[name]
    raise ConfigError(f"unsupported secret source: {source}")


def _credential(value: str, *, interactive: bool) -> TemporaryCredential:
    credential_id, specification = _parse_pair(value, "--credential")
    if specification.startswith("secret-text:"):
        source = specification[len("secret-text:") :]
        return TemporaryCredential(
            credential_id,
            "secret-text",
            _secret_source(
                source,
                interactive=interactive,
                prompt=f"Secret for {credential_id}",
            ),
        )
    if specification == "username-password:prompt":
        if not interactive:
            raise InteractionError("username/password prompt is unavailable")
        username = str(click.prompt(f"Username for {credential_id}"))
        password = str(click.prompt(f"Password for {credential_id}", hide_input=True))
        return TemporaryCredential(credential_id, "username-password", password, username)
    prefix = "username-password:env:"
    if specification.startswith(prefix):
        names = specification[len(prefix) :].split(",")
        if len(names) != 2 or any(not name or name not in os.environ for name in names):
            raise InteractionError(
                "username-password env source requires two available variables: "
                "USER_VAR,PASSWORD_VAR"
            )
        return TemporaryCredential(
            credential_id,
            "username-password",
            os.environ[names[1]],
            os.environ[names[0]],
        )
    raise ConfigError(
        "credential must use secret-text:prompt|stdin|env:NAME, "
        "username-password:prompt, or username-password:env:USER_VAR,PASSWORD_VAR"
    )


def _resolve_learned_parameters(
    paths: ProjectPaths,
    configured: set[str],
    parameters: dict[str, str],
    secret_parameters: dict[str, str],
    *,
    interactive: bool,
) -> None:
    missing: list[str] = []
    for definition in load_parameter_definitions(paths):
        name = str(definition.get("name") or "")
        if (
            not name
            or name.startswith("LOL_")
            or name in configured
            or name in parameters
            or name in secret_parameters
            or isinstance(definition.get("defaultParameterValue"), dict)
        ):
            continue
        if not interactive:
            missing.append(name)
            continue
        kind = str(definition.get("_class") or "")
        if kind.endswith("PasswordParameterDefinition"):
            secret_parameters[name] = str(
                click.prompt(f"Secret pipeline parameter {name}", hide_input=True)
            )
        else:
            parameters[name] = str(click.prompt(f"Pipeline parameter {name}"))
    if missing:
        raise InteractionError(
            "required pipeline parameters are missing: "
            + ", ".join(sorted(missing))
            + "; supply --parameter or --secret-parameter"
        )


@cli.command("run")
@click.option("--revision", help="Run a committed Git revision instead of the working tree.")
@click.option("--jenkinsfile", help="Use a repository-relative Jenkinsfile for this run.")
@click.option("--parameter", "parameter_values", multiple=True, metavar="KEY=VALUE")
@click.option(
    "--secret-parameter",
    "secret_values",
    multiple=True,
    metavar="KEY=prompt|stdin|env:NAME",
)
@click.option("--credential", "credential_values", multiple=True)
@click.option("--trust-repository", is_flag=True, help="Record trust without prompting.")
@click.option("--non-interactive", is_flag=True, help="Disable every prompt.")
@click.option("--controller-timeout", type=click.FloatRange(min=0.1), default=120.0)
@click.option("--queue-timeout", type=click.FloatRange(min=0.1), default=60.0)
@click.pass_obj
def run_command(
    context: Context,
    revision: str | None,
    jenkinsfile: str | None,
    parameter_values: tuple[str, ...],
    secret_values: tuple[str, ...],
    credential_values: tuple[str, ...],
    trust_repository: bool,
    non_interactive: bool,
    controller_timeout: float,
    queue_timeout: float,
) -> None:
    """Snapshot the repository and run its Jenkinsfile."""
    config, identity, paths = _resolved_project()
    prompts_allowed = not non_interactive and context.output_format != "json" and _is_interactive()
    if not is_trusted(paths.state, identity):
        if trust_repository:
            trust(paths.state, identity)
        elif not prompts_allowed:
            raise InteractionError(
                "repository is not trusted; review it and pass --trust-repository explicitly"
            )
        else:
            click.echo(
                "This Jenkinsfile executes with your user privileges and can access your files "
                "and host commands."
            )
            if not click.confirm("Trust and run this repository?", default=False):
                raise InteractionError("repository was not trusted")
            trust(paths.state, identity)
    if jenkinsfile:
        candidate = Path(jenkinsfile)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ConfigError("--jenkinsfile must be a repository-relative path without '..'")
    parameter_pairs = [_parse_pair(item, "--parameter") for item in parameter_values]
    if len({key for key, _ in parameter_pairs}) != len(parameter_pairs):
        raise ConfigError("--parameter keys must be unique")
    parameters = dict(parameter_pairs)
    secret_pairs = [_parse_pair(item, "--secret-parameter") for item in secret_values]
    if len({key for key, _ in secret_pairs}) != len(secret_pairs):
        raise ConfigError("--secret-parameter keys must be unique")
    collisions = set(parameters) & {key for key, _ in secret_pairs}
    if collisions:
        raise ConfigError(
            "a parameter cannot be both secret and non-secret: " + ", ".join(sorted(collisions))
        )
    supplied_reserved = (set(parameters) | {key for key, _ in secret_pairs}) & RESERVED_PARAMETERS
    if supplied_reserved:
        raise ConfigError("reserved LOL parameter: " + ", ".join(sorted(supplied_reserved)))
    secret_parameters = {
        key: _secret_source(
            source,
            interactive=prompts_allowed,
            prompt=f"Secret pipeline parameter {key}",
        )
        for key, source in secret_pairs
    }
    configured = {str(key) for key in config.values["pipeline"].get("parameters", {})}
    _resolve_learned_parameters(
        paths,
        configured,
        parameters,
        secret_parameters,
        interactive=prompts_allowed,
    )
    temporary = [_credential(item, interactive=prompts_allowed) for item in credential_values]
    identifiers = [item.credential_id for item in temporary]
    if len(identifiers) != len(set(identifiers)):
        raise ConfigError("--credential IDs must be unique")
    if context.output_format != "json":
        click.echo("Run plan:")
        click.echo(f"  Repository: {config.root}")
        click.echo(f"  Revision: {revision or 'working tree snapshot'}")
        click.echo(f"  Jenkinsfile: {jenkinsfile or config.values['pipeline']['file']}")
        click.echo(f"  Parameters: {', '.join(sorted(parameters)) or '-'}")
        click.echo(f"  Secret parameters: {', '.join(sorted(secret_parameters)) or '-'}")
        click.echo(f"  Temporary credentials: {', '.join(sorted(identifiers)) or '-'}")
    emit = (
        (lambda value: click.echo(value, nl=False))
        if context.output_format == "text"
        else (lambda _: None)
    )
    exit_code, record = execute(
        config,
        identity,
        paths,
        revision=revision,
        jenkinsfile=jenkinsfile,
        parameters=parameters,
        secret_parameters=secret_parameters,
        temporary_credentials=temporary,
        emit=emit,
        controller_timeout=controller_timeout,
        queue_timeout=queue_timeout,
    )
    if context.output_format == "json":
        context.emit({"run": record.metadata})
    else:
        click.echo(f"Run {record.run_id}: {record.metadata.get('result')}")
    if exit_code:
        raise LolError(f"pipeline result: {record.metadata.get('result')}", exit_code)


@cli.command("runs")
@click.pass_obj
def runs_command(context: Context) -> None:
    """List recorded runs."""
    _, _, paths = _resolved_project()
    records = [record.metadata for record in list_runs(paths)]
    if context.output_format == "json":
        context.emit({"runs": records})
    elif not records:
        click.echo("No runs recorded.")
    else:
        for value in records:
            click.echo(f"{value['run_id']}  {value.get('status', '-')}  {value.get('result', '-')}")


@cli.command("logs")
@click.option("--run", "run_id")
@click.option("--follow", is_flag=True)
@click.pass_obj
def logs_command(context: Context, run_id: str | None, follow: bool) -> None:
    """Read or follow redacted pipeline console output."""
    if follow and context.output_format == "json":
        raise ConfigError("--format json cannot be combined with logs --follow")
    _, _, paths = _resolved_project()
    record = _select_record(paths, run_id, latest=True)
    offset = 0
    collected: list[str] = []
    while True:
        chunk, offset = read_console(record, offset)
        if chunk:
            if context.output_format == "json":
                collected.append(chunk)
            else:
                click.echo(chunk, nl=False)
        if not follow:
            break
        record = load_run(paths, record.run_id)
        if record.metadata.get("status") not in {"provisioning", "queued", "running", "stopping"}:
            break
        if controller_status(paths).state != "running":
            break
        time.sleep(0.25)
    if context.output_format == "json":
        context.emit({"run_id": record.run_id, "console": "".join(collected)})


@cli.command("stop")
@click.option("--run", "run_id")
@click.option("--yes", is_flag=True)
@click.pass_obj
def stop_command(context: Context, run_id: str | None, yes: bool) -> None:
    """Cancel an active pipeline or queued build."""
    _, identity, paths = _resolved_project()
    record = _select_record(
        paths,
        run_id,
        active=True,
        allow_prompt=context.output_format != "json",
    )
    if not yes:
        if context.output_format == "json" or not _is_interactive():
            raise InteractionError(f"stopping run {record.run_id} requires confirmation; use --yes")
        if not click.confirm(f"Stop run {record.run_id}?", default=False):
            raise InteractionError("cancellation cancelled")
    current = controller_status(paths)
    if current.state != "running" or not current.endpoint:
        raise ConfigError("controller is not running")
    username, password = controller_credentials(paths)
    previous_status = str(record.metadata.get("status") or "running")
    record.update(status="stopping")
    try:
        with JenkinsClient(current.endpoint, username, password) as client:
            build_url = record.metadata.get("jenkins_url")
            queue_url = record.metadata.get("jenkins_queue")
            if isinstance(build_url, str) and build_url:
                client.stop(build_url)
            elif isinstance(queue_url, str) and queue_url:
                client.cancel_queue(queue_url)
            else:
                raise ConfigError(f"run cannot yet be cancelled: {record.run_id}")
    except Exception:
        record.update(status=previous_status)
        raise
    context.emit({"project_id": identity.project_id, "run_id": record.run_id, "status": "stopping"})


@cli.command("artifacts")
@click.option("--run", "run_id")
@click.option("--output", type=click.Path(path_type=Path))
@click.pass_obj
def artifacts_command(context: Context, run_id: str | None, output: Path | None) -> None:
    """List or copy downloaded run artifacts."""
    _, _, paths = _resolved_project()
    record = _select_record(paths, run_id, allow_prompt=context.output_format != "json")
    if output:
        copied = copy_artifacts(record, output)
        context.emit({"run_id": record.run_id, "copied": [str(path) for path in copied]})
    else:
        value = load_artifact_index(record)
        value["run_id"] = record.run_id
        context.emit(value)


@cli.command("reset")
@click.option("--yes", is_flag=True, help="Confirm removal of the displayed generated state.")
@click.pass_obj
def reset_command(context: Context, yes: bool) -> None:
    """Recreate generated controller state from pinned configuration."""
    config, identity, paths = _resolved_project()
    targets = [str(paths.controller), str(paths.runtime)]
    if context.output_format != "json":
        click.echo("Generated controller state will be removed and recreated:")
        for target in targets:
            click.echo(f"  {target}")
        click.echo(f"Preserved run history: {paths.runs}")
    if not yes:
        if context.output_format == "json" or not _is_interactive():
            raise InteractionError(
                "reset confirmation is unavailable; review the targets and use --yes"
            )
        if not click.confirm("Reset this generated controller state?", default=False):
            raise InteractionError("reset cancelled")
    controller_reset(paths)
    result = controller_up(config, paths)
    payload = _controller_payload(identity, result)
    payload["reset_targets"] = targets
    payload["preserved_runs"] = str(paths.runs)
    context.emit(payload)


def main() -> None:
    try:
        cli(standalone_mode=False)
    except KeyboardInterrupt:
        click.echo("Interrupted.", err=True)
        raise SystemExit(EXIT_INTERRUPTED) from None
    except click.Abort:
        click.echo("Interrupted.", err=True)
        raise SystemExit(EXIT_INTERRUPTED) from None
    except click.ClickException as exc:
        exc.show()
        raise SystemExit(EXIT_USAGE) from None
    except LolError as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(exc.exit_code) from None
    except (FileNotFoundError, KeyError, ValueError) as exc:
        click.echo(f"Error: {exc}", err=True)
        raise SystemExit(EXIT_USAGE) from None
    raise SystemExit(EXIT_SUCCESS)
