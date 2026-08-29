from __future__ import annotations

import difflib
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

import click
import yaml

from lol import __version__
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
    EXIT_INTERRUPTED,
    EXIT_SUCCESS,
    EXIT_USAGE,
    LOCK_NAME,
    MANIFEST_NAME,
    PINNED_JENKINS_VERSION,
)
from lol.discovery import discover, initial_manifest
from lol.errors import ConfigError, InteractionError, LolError
from lol.io import write_yaml, write_yaml_bundle
from lol.lockfile import create_lock, load_lock
from lol.project import find_repository


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
    context.emit({"configuration": redact(config.values), "sources": config.sources})


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
@click.option("--detected-labels/--no-detected-labels", default=None)
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
