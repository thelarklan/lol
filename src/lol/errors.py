from __future__ import annotations

from dataclasses import dataclass

from lol.constants import EXIT_HARNESS, EXIT_USAGE


@dataclass(slots=True)
class LolError(Exception):
    message: str
    exit_code: int = EXIT_HARNESS

    def __str__(self) -> str:
        return self.message


class ConfigError(LolError):
    def __init__(self, message: str) -> None:
        super().__init__(message, EXIT_USAGE)


class InteractionError(ConfigError):
    pass


class HarnessError(LolError):
    pass
