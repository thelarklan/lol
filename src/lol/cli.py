from __future__ import annotations

import json
from typing import Any

import click
import yaml

from lol import __version__
from lol.config import load_effective, redact
from lol.constants import EXIT_INTERRUPTED, EXIT_SUCCESS, EXIT_USAGE
from lol.errors import LolError


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


def main() -> None:
    try:
        cli(standalone_mode=False)
    except KeyboardInterrupt:
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
