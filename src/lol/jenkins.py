from __future__ import annotations

import re
import time
import urllib.parse
import xml.sax.saxutils as xml_escape
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import requests

from lol.errors import HarnessError

HEADER_NAME = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
RESERVED_HEADERS = {"authorization", "content-length", "content-type", "cookie", "host"}


@dataclass(frozen=True, slots=True)
class Build:
    number: int
    url: str
    building: bool
    result: str | None


class JenkinsClient:
    def __init__(
        self,
        endpoint: str,
        username: str,
        password: str,
        timeout: float = 20.0,
    ) -> None:
        if timeout <= 0:
            raise HarnessError("Jenkins request timeout must be greater than zero")
        self.endpoint = self._validated_endpoint(endpoint)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.trust_env = False
        self._crumb: tuple[str, str] | None = None

    @staticmethod
    def _validated_endpoint(value: str) -> str:
        try:
            parsed = urllib.parse.urlsplit(value)
            port = parsed.port
        except ValueError as exc:
            raise HarnessError("Jenkins endpoint is invalid") from exc
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or port is None
            or not 1 <= port <= 65535
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise HarnessError("Jenkins endpoint must be an HTTP loopback address with a port")
        return f"http://127.0.0.1:{port}"

    def close(self) -> None:
        self.session.close()

    def __enter__(self) -> JenkinsClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _controller_url(self, value: str, action: str) -> str:
        candidate = urllib.parse.urljoin(f"{self.endpoint}/", value)
        try:
            expected = urllib.parse.urlsplit(self.endpoint)
            parsed = urllib.parse.urlsplit(candidate)
            port = parsed.port
        except ValueError as exc:
            raise HarnessError(f"could not {action}: Jenkins returned an invalid URL") from exc
        if (
            parsed.scheme != expected.scheme
            or parsed.hostname != expected.hostname
            or port != expected.port
            or parsed.username is not None
            or parsed.password is not None
            or parsed.fragment
        ):
            raise HarnessError(f"could not {action}: Jenkins returned a non-controller URL")
        return urllib.parse.urlunsplit(parsed)

    def _url(self, path: str) -> str:
        return self._controller_url(path.lstrip("/"), f"resolve {path}")

    @staticmethod
    def _check(response: requests.Response, action: str) -> None:
        if response.status_code >= 400:
            raise HarnessError(f"could not {action}: HTTP {response.status_code}")

    @staticmethod
    def _json_object(response: requests.Response, action: str) -> dict[str, Any]:
        try:
            value = response.json()
        except ValueError as exc:
            raise HarnessError(f"could not {action}: Jenkins returned invalid JSON") from exc
        if not isinstance(value, dict):
            raise HarnessError(f"could not {action}: Jenkins returned non-object JSON")
        return value

    def get_response(
        self,
        target: str,
        *,
        action: str,
        stream: bool = False,
    ) -> requests.Response:
        url = self._controller_url(target, action)
        try:
            response = self.session.get(
                url,
                stream=stream,
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise HarnessError(f"could not {action}: Jenkins is unavailable") from exc
        self._check(response, action)
        return response

    def _headers(self) -> dict[str, str]:
        if self._crumb is None:
            try:
                response = self.session.get(
                    self._url("crumbIssuer/api/json"),
                    timeout=self.timeout,
                    allow_redirects=False,
                )
            except requests.RequestException as exc:
                raise HarnessError(
                    "could not obtain a Jenkins crumb: Jenkins is unavailable"
                ) from exc
            if response.status_code == 404:
                self._crumb = ("", "")
            else:
                self._check(response, "obtain a Jenkins crumb")
                value = self._json_object(response, "obtain a Jenkins crumb")
                field = value.get("crumbRequestField")
                crumb = value.get("crumb")
                if (
                    not isinstance(field, str)
                    or not field
                    or not HEADER_NAME.fullmatch(field)
                    or field.lower() in RESERVED_HEADERS
                    or not isinstance(crumb, str)
                    or not crumb
                ):
                    raise HarnessError("could not obtain a Jenkins crumb: response is incomplete")
                self._crumb = (field, crumb)
        return {self._crumb[0]: self._crumb[1]} if self._crumb[0] else {}

    def get_json(self, path: str) -> dict[str, Any]:
        response = self.get_response(self._url(path), action=f"GET {path}")
        return self._json_object(response, f"GET {path}")

    def post(
        self,
        path: str,
        *,
        data: dict[str, str] | str | None = None,
        content_type: str | None = None,
    ) -> requests.Response:
        headers = self._headers()
        if content_type:
            headers["Content-Type"] = content_type
        try:
            response = self.session.post(
                self._url(path),
                data=data,
                headers=headers,
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise HarnessError(f"could not POST {path}: Jenkins is unavailable") from exc
        self._check(response, f"POST {path}")
        return response

    def ensure_pipeline_job(
        self,
        name: str,
        repository_url: str,
        branch: str,
        script: str,
        parameters: list[str] | None = None,
        secret_parameters: list[str] | None = None,
    ) -> None:
        encoded = urllib.parse.quote(name, safe="")
        try:
            response = self.session.get(
                self._url(f"job/{encoded}/api/json"),
                timeout=self.timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise HarnessError("could not inspect Pipeline job: Jenkins is unavailable") from exc

        learned: dict[str, dict[str, Any]] = {}
        if response.status_code != 404:
            self._check(response, "inspect Pipeline job")
            learned = {
                str(item.get("name")): item
                for item in self.parameter_definitions(name)
                if item.get("name") and not str(item.get("name")).startswith("LOL_")
            }
        for parameter in parameters or ():
            learned.setdefault(parameter, {"name": parameter})
        for parameter in secret_parameters or ():
            learned[parameter] = {
                "name": parameter,
                "_class": "hudson.model.PasswordParameterDefinition",
            }
        internal = {
            parameter: {"name": parameter}
            for parameter in (
                "LOL_PROJECT_ID",
                "LOL_RUN_ID",
                "LOL_CONTROLLER_URL",
                "LOL_WORKSPACE",
            )
        }
        definitions = [
            self._parameter_xml(item) for _, item in sorted({**learned, **internal}.items())
        ]
        rendered_definitions = "\n      ".join(definitions)
        escape = xml_escape.escape
        xml = f"""<?xml version='1.1' encoding='UTF-8'?>
<flow-definition plugin="workflow-job">
  <actions/>
  <description>Managed by LOL</description>
  <keepDependencies>false</keepDependencies>
  <properties>
    <jenkins.model.BuildDiscarderProperty>
      <strategy class="hudson.tasks.LogRotator">
        <daysToKeep>30</daysToKeep><numToKeep>50</numToKeep>
      </strategy>
    </jenkins.model.BuildDiscarderProperty>
    <hudson.model.ParametersDefinitionProperty><parameterDefinitions>
      {rendered_definitions}
    </parameterDefinitions></hudson.model.ParametersDefinitionProperty>
  </properties>
  <definition class="org.jenkinsci.plugins.workflow.cps.CpsScmFlowDefinition" plugin="workflow-cps">
    <scm class="hudson.plugins.git.GitSCM" plugin="git">
      <configVersion>2</configVersion>
      <userRemoteConfigs><hudson.plugins.git.UserRemoteConfig><url>{escape(repository_url)}</url></hudson.plugins.git.UserRemoteConfig></userRemoteConfigs>
      <branches><hudson.plugins.git.BranchSpec><name>*/{escape(branch)}</name></hudson.plugins.git.BranchSpec></branches>
      <doGenerateSubmoduleConfigurations>false</doGenerateSubmoduleConfigurations>
      <submoduleCfg class="empty-list"/>
      <extensions><hudson.plugins.git.extensions.impl.CloneOption><shallow>false</shallow><noTags>false</noTags><honorRefspec>true</honorRefspec></hudson.plugins.git.extensions.impl.CloneOption></extensions>
    </scm>
    <scriptPath>{escape(script)}</scriptPath>
    <lightweight>false</lightweight>
  </definition>
  <disabled>false</disabled>
</flow-definition>
"""
        if response.status_code == 404:
            self.post(
                f"createItem?name={encoded}",
                data=xml,
                content_type="application/xml; charset=utf-8",
            )
        else:
            self.post(
                f"job/{encoded}/config.xml",
                data=xml,
                content_type="application/xml; charset=utf-8",
            )

    @staticmethod
    def _parameter_xml(definition: dict[str, Any]) -> str:
        escape = xml_escape.escape
        name = escape(str(definition["name"]))
        kind = str(definition.get("_class") or "hudson.model.StringParameterDefinition")
        default = definition.get("defaultParameterValue")
        default_value = default.get("value") if isinstance(default, dict) else ""
        if kind.endswith("BooleanParameterDefinition"):
            enabled = default_value is True or str(default_value).lower() == "true"
            value = "true" if enabled else "false"
            return (
                "<hudson.model.BooleanParameterDefinition>"
                f"<name>{name}</name><defaultValue>{value}</defaultValue>"
                "</hudson.model.BooleanParameterDefinition>"
            )
        if kind.endswith("ChoiceParameterDefinition"):
            choices = definition.get("choices")
            if not isinstance(choices, list):
                choices = []
            values = "".join(f"<string>{escape(str(value))}</string>" for value in choices)
            return (
                "<hudson.model.ChoiceParameterDefinition>"
                f'<name>{name}</name><choices class="java.util.Arrays$ArrayList">'
                f'<a class="string-array">{values}</a></choices>'
                "</hudson.model.ChoiceParameterDefinition>"
            )
        if kind.endswith("PasswordParameterDefinition"):
            return (
                "<hudson.model.PasswordParameterDefinition>"
                f"<name>{name}</name><defaultValue></defaultValue>"
                "</hudson.model.PasswordParameterDefinition>"
            )
        tag = (
            "hudson.model.TextParameterDefinition"
            if kind.endswith("TextParameterDefinition")
            else "hudson.model.StringParameterDefinition"
        )
        trim = "<trim>false</trim>" if tag.endswith("StringParameterDefinition") else ""
        return (
            f"<{tag}><name>{name}</name>"
            f"<defaultValue>{escape(str(default_value or ''))}</defaultValue>{trim}</{tag}>"
        )

    def trigger(self, job: str, parameters: dict[str, str]) -> str:
        encoded = urllib.parse.quote(job, safe="")
        action = "buildWithParameters" if parameters else "build"
        response = self.post(f"job/{encoded}/{action}", data=parameters)
        location = response.headers.get("Location")
        if not location:
            raise HarnessError("Jenkins did not return a queue location")
        return self._controller_url(location, "resolve Jenkins queue location")

    def wait_for_build(self, queue_url: str, timeout: float = 60.0) -> Build:
        if timeout <= 0:
            raise HarnessError("Jenkins queue timeout must be greater than zero")
        queue = self._controller_url(queue_url, "read Jenkins queue")
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            response = self.get_response(
                f"{queue.rstrip('/')}/api/json", action="read Jenkins queue item"
            )
            value = self._json_object(response, "read Jenkins queue item")
            if value.get("cancelled"):
                raise HarnessError("Jenkins cancelled the queued build")
            executable = value.get("executable")
            if isinstance(executable, dict):
                try:
                    number = int(executable["number"])
                    url = self._controller_url(
                        str(executable["url"]), "resolve Jenkins build location"
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise HarnessError("Jenkins returned an invalid executable queue item") from exc
                return Build(number, url, True, None)
            time.sleep(0.25)
        raise HarnessError("timed out waiting for Jenkins to schedule the build")

    def build(self, url: str) -> Build:
        build_url = self._controller_url(url, "read Jenkins build")
        response = self.get_response(
            f"{build_url.rstrip('/')}/api/json", action="read Jenkins build"
        )
        value = self._json_object(response, "read Jenkins build")
        try:
            number = int(value["number"])
            returned_url = self._controller_url(str(value["url"]), "resolve Jenkins build")
            raw_building = value["building"]
            if not isinstance(raw_building, bool):
                raise TypeError("building is not boolean")
            building = raw_building
        except (KeyError, TypeError, ValueError) as exc:
            raise HarnessError("Jenkins returned invalid build metadata") from exc
        return Build(
            number,
            returned_url,
            building,
            str(value["result"]) if value.get("result") is not None else None,
        )

    def console_chunks(self, build_url: str, *, follow: bool = True) -> Iterator[str]:
        build = self._controller_url(build_url, "stream Jenkins console")
        offset = 0
        while True:
            response = self.get_response(
                f"{build.rstrip('/')}/logText/progressiveText?start={offset}",
                action="stream Jenkins console",
            )
            if response.text:
                yield response.text
            try:
                next_offset = int(
                    response.headers.get("X-Text-Size", offset + len(response.content))
                )
            except ValueError as exc:
                raise HarnessError("Jenkins returned an invalid console offset") from exc
            if next_offset < offset:
                raise HarnessError("Jenkins returned a regressing console offset")
            offset = next_offset
            more = response.headers.get("X-More-Data", "false").lower() == "true"
            if not follow or not more:
                return
            time.sleep(0.25)

    def stop(self, build_url: str) -> None:
        build = self._controller_url(build_url, "stop Jenkins build")
        path = urllib.parse.urlsplit(build).path.strip("/")
        if not path:
            raise HarnessError("could not stop Jenkins build: build URL has no path")
        self.post(f"{path}/stop")

    def parameter_definitions(self, job: str) -> list[dict[str, Any]]:
        encoded = urllib.parse.quote(job, safe="")
        value = self.get_json(f"job/{encoded}/api/json?tree=property[parameterDefinitions[*]]")
        properties = value.get("property", [])
        if not isinstance(properties, list):
            return []
        for prop in properties:
            if not isinstance(prop, dict):
                continue
            definitions = prop.get("parameterDefinitions")
            if isinstance(definitions, list):
                return [item for item in definitions if isinstance(item, dict)]
        return []

    @staticmethod
    def _credential_id(value: str) -> str:
        if not value or value.strip() != value:
            raise HarnessError("Jenkins credential ID must be non-empty without outer whitespace")
        return value

    def create_secret_text(self, credential_id: str, secret: str, run_id: str) -> None:
        escape = xml_escape.escape
        identifier = self._credential_id(credential_id)
        xml = f"""<org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl>
<scope>GLOBAL</scope><id>{escape(identifier)}</id><description>lol:{escape(run_id)}</description><secret>{escape(secret)}</secret>
</org.jenkinsci.plugins.plaincredentials.impl.StringCredentialsImpl>"""
        self.post(
            "credentials/store/system/domain/_/createCredentials",
            data=xml,
            content_type="application/xml; charset=utf-8",
        )

    def credential_descriptions(self) -> dict[str, str]:
        value = self.get_json(
            "credentials/store/system/domain/_/api/json?tree=credentials[id,description]"
        )
        credentials = value.get("credentials", [])
        if not isinstance(credentials, list):
            return {}
        return {
            str(item["id"]): str(item.get("description") or "")
            for item in credentials
            if isinstance(item, dict) and item.get("id")
        }

    def create_username_password(
        self,
        credential_id: str,
        username: str,
        password: str,
        run_id: str,
    ) -> None:
        escape = xml_escape.escape
        identifier = self._credential_id(credential_id)
        xml = f"""<com.cloudbees.plugins.credentials.impl.UsernamePasswordCredentialsImpl>
<scope>GLOBAL</scope><id>{escape(identifier)}</id><description>lol:{escape(run_id)}</description><username>{escape(username)}</username><password>{escape(password)}</password>
</com.cloudbees.plugins.credentials.impl.UsernamePasswordCredentialsImpl>"""
        self.post(
            "credentials/store/system/domain/_/createCredentials",
            data=xml,
            content_type="application/xml; charset=utf-8",
        )

    def delete_credential(self, credential_id: str) -> None:
        identifier = self._credential_id(credential_id)
        encoded = urllib.parse.quote(identifier, safe="")
        self.post(f"credentials/store/system/domain/_/credential/{encoded}/doDelete")
