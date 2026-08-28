from __future__ import annotations

APP_NAME = "lol"
MANIFEST_NAME = "lol.yaml"
SCHEMA_VERSION = 1

PINNED_JENKINS_VERSION = "2.568.2"

DEFAULT_PLUGINS = (
    "configuration-as-code",
    "credentials-binding",
    "git",
    "plain-credentials",
    "workflow-aggregator",
)

EXIT_SUCCESS = 0
EXIT_USAGE = 2
EXIT_HARNESS = 4
EXIT_INTERRUPTED = 130
