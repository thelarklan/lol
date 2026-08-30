from __future__ import annotations

APP_NAME = "lol"
MANIFEST_NAME = "lol.yaml"
LOCK_NAME = "lol.plugins.lock.yaml"
SCHEMA_VERSION = 1

PINNED_JENKINS_VERSION = "2.568.2"
PINNED_JENKINS_SHA256 = "9bbb2b329e52730ba7decd1a7a1095987f6250ec761fb21157dbb2cbcd1ef590"
PINNED_JENKINS_URL = f"https://get.jenkins.io/war-stable/{PINNED_JENKINS_VERSION}/jenkins.war"

PLUGIN_MANAGER_VERSION = "2.15.0"
PLUGIN_MANAGER_SHA256 = "a86853ec2e2933f37a4b471ba65099b61e03c87a80c2ef8fe2315eb135672d43"
PLUGIN_MANAGER_URL = (
    "https://github.com/jenkinsci/plugin-installation-manager-tool/releases/download/"
    f"{PLUGIN_MANAGER_VERSION}/jenkins-plugin-manager-{PLUGIN_MANAGER_VERSION}.jar"
)

# Keep runtime compatibility release-pinned instead of accepting every newer
# Java feature release without qualification against this Jenkins LTS.
SUPPORTED_JAVA_VERSIONS = (21, 25)

DEFAULT_PLUGINS = (
    "configuration-as-code",
    "credentials-binding",
    "git",
    "plain-credentials",
    "workflow-aggregator",
)

EXIT_SUCCESS = 0
EXIT_USAGE = 2
EXIT_HOST = 3
EXIT_HARNESS = 4
EXIT_INTERRUPTED = 130
