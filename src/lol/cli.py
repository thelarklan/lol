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
from lol.config import load_effective, redact
from lol.constants import EXIT_INTERRUPTED, EXIT_SUCCESS, EXIT_USAGE, LOCK_NAME
from lol.errors import InteractionError, LolError
from lol.io import write_yaml
from lol.lockfile import create_lock, load_lock


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
    before = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
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


def _is_interactive_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


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
            if not _is_interactive_terminal():
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
