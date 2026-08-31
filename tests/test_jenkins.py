from __future__ import annotations

from collections.abc import Callable, Iterator
from typing import Any, cast

import pytest
import requests

from lol.errors import HarnessError
from lol.jenkins import JenkinsClient


class Response:
    def __init__(
        self,
        status_code: int = 200,
        payload: Any = None,
        *,
        headers: dict[str, str] | None = None,
        text: str = "",
        chunks: tuple[bytes, ...] = (),
    ) -> None:
        self.status_code = status_code
        self.payload = {} if payload is None else payload
        self.headers = headers or {}
        self.text = text
        self.content = text.encode()
        self.chunks = chunks
        self.closed = False

    def json(self) -> Any:
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload

    def iter_content(self, _: int) -> Iterator[bytes]:
        yield from self.chunks

    def close(self) -> None:
        self.closed = True


class Session:
    def __init__(self, responder: Callable[[str], Response]) -> None:
        self.auth: tuple[str, str] | None = None
        self.trust_env = True
        self.responder = responder
        self.gets: list[str] = []
        self.get_options: list[dict[str, object]] = []
        self.posts: list[tuple[str, object, dict[str, str]]] = []
        self.post_options: list[dict[str, object]] = []
        self.closed = False

    def get(self, url: str, **options: object) -> Response:
        self.gets.append(url)
        self.get_options.append(options)
        return self.responder(url)

    def post(
        self,
        url: str,
        *,
        data: object = None,
        headers: dict[str, str] | None = None,
        **options: object,
    ) -> Response:
        self.posts.append((url, data, headers or {}))
        self.post_options.append(options)
        return self.responder(url)

    def close(self) -> None:
        self.closed = True


def client_with(session: Session) -> JenkinsClient:
    client = JenkinsClient("http://127.0.0.1:8080", "lol", "password")
    client.session.close()
    client.session = cast(Any, session)
    return client


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://127.0.0.1:8080",
        "http://localhost:8080",
        "http://127.0.0.1",
        "http://127.0.0.1:0",
        "http://user@127.0.0.1:8080",
        "http://127.0.0.1:8080/path",
    ],
)
def test_client_rejects_non_controller_endpoint(endpoint: str) -> None:
    with pytest.raises(HarnessError, match="HTTP loopback"):
        JenkinsClient(endpoint, "lol", "password")


def test_client_uses_authenticated_proxy_free_session() -> None:
    client = JenkinsClient("http://127.0.0.1:8080/", "lol", "password")
    try:
        assert client.endpoint == "http://127.0.0.1:8080"
        assert client.session.auth == ("lol", "password")
        assert not client.session.trust_env
    finally:
        client.close()


def test_post_fetches_and_reuses_valid_crumb() -> None:
    def respond(url: str) -> Response:
        if "crumbIssuer" in url:
            return Response(payload={"crumbRequestField": "Jenkins-Crumb", "crumb": "value"})
        return Response()

    session = Session(respond)
    client = client_with(session)

    client.post("job/one/build")
    client.post("job/two/build")

    assert sum("crumbIssuer" in url for url in session.gets) == 1
    assert session.posts[0][2] == {"Jenkins-Crumb": "value"}
    assert session.posts[1][2] == {"Jenkins-Crumb": "value"}
    assert all(options["allow_redirects"] is False for options in session.get_options)
    assert all(options["allow_redirects"] is False for options in session.post_options)


@pytest.mark.parametrize("field", ["Bad Header", "Host", "Authorization"])
def test_post_rejects_invalid_crumb_header_name(field: str) -> None:
    session = Session(lambda url: Response(payload={"crumbRequestField": field, "crumb": "value"}))
    client = client_with(session)

    with pytest.raises(HarnessError, match="response is incomplete"):
        client.post("job/one/build")

    assert session.posts == []


def test_http_error_does_not_echo_response_body() -> None:
    response = Response(500, text="secret response body")
    with pytest.raises(HarnessError, match="HTTP 500") as exc_info:
        JenkinsClient._check(cast(requests.Response, response), "perform action")
    assert "secret response body" not in str(exc_info.value)


def test_job_reconciliation_preserves_parameter_types_and_escapes_xml() -> None:
    def respond(url: str) -> Response:
        if "crumbIssuer" in url:
            return Response(payload={"crumbRequestField": "Crumb", "crumb": "value"})
        if "parameterDefinitions" in url:
            return Response(
                payload={
                    "property": [
                        {
                            "parameterDefinitions": [
                                {
                                    "name": "ENABLED",
                                    "_class": "hudson.model.BooleanParameterDefinition",
                                    "defaultParameterValue": {"value": False},
                                },
                                {
                                    "name": "TARGET",
                                    "_class": "hudson.model.ChoiceParameterDefinition",
                                    "choices": ["one", "two&three"],
                                },
                            ]
                        }
                    ]
                }
            )
        return Response()

    session = Session(respond)
    client = client_with(session)

    client.ensure_pipeline_job(
        "fixture",
        "git://127.0.0.1/repository.git?x=1&y=2",
        "lol&run",
        "ci/Jenkinsfile&more",
        parameters=["GREETING"],
        secret_parameters=["TOKEN"],
    )

    xml = str(session.posts[-1][1])
    assert "<hudson.model.BooleanParameterDefinition>" in xml
    assert "<defaultValue>false</defaultValue>" in xml
    assert "<string>two&amp;three</string>" in xml
    assert "<name>TOKEN</name><defaultValue></defaultValue>" in xml
    assert "repository.git?x=1&amp;y=2" in xml
    assert "*/lol&amp;run" in xml
    assert "ci/Jenkinsfile&amp;more" in xml
    assert xml.count("<name>LOL_RUN_ID</name>") == 1


def test_trigger_rejects_queue_location_on_another_origin() -> None:
    def respond(url: str) -> Response:
        if "crumbIssuer" in url:
            return Response(payload={"crumbRequestField": "Crumb", "crumb": "value"})
        return Response(headers={"Location": "http://example.com/queue/item/1/"})

    client = client_with(Session(respond))
    with pytest.raises(HarnessError, match="non-controller URL"):
        client.trigger("fixture", {})


def test_wait_for_build_rejects_external_executable_url() -> None:
    session = Session(
        lambda url: Response(
            payload={"executable": {"number": 1, "url": "http://example.com/job/x/1/"}}
        )
    )
    client = client_with(session)

    with pytest.raises(HarnessError, match="non-controller URL"):
        client.wait_for_build("http://127.0.0.1:8080/queue/item/1/", timeout=1)


def test_build_and_console_stream_validate_metadata() -> None:
    def respond(url: str) -> Response:
        if url.endswith("/api/json"):
            return Response(
                payload={
                    "number": 7,
                    "url": "http://127.0.0.1:8080/job/fixture/7/",
                    "building": False,
                    "result": "SUCCESS",
                }
            )
        if "start=0" in url:
            return Response(headers={"X-Text-Size": "5", "X-More-Data": "true"}, text="first")
        return Response(headers={"X-Text-Size": "11"}, text="second")

    client = client_with(Session(respond))
    build = client.build("http://127.0.0.1:8080/job/fixture/7/")
    chunks = list(client.console_chunks(build.url))

    assert build.number == 7
    assert build.result == "SUCCESS"
    assert chunks == ["first", "second"]


def test_console_rejects_text_without_offset_progress() -> None:
    client = client_with(
        Session(
            lambda url: Response(
                headers={"X-Text-Size": "0", "X-More-Data": "true"}, text="duplicate"
            )
        )
    )

    with pytest.raises(HarnessError, match="without advancing"):
        list(client.console_chunks("http://127.0.0.1:8080/job/fixture/7/"))


def test_credentials_are_escaped_and_delete_id_is_encoded() -> None:
    def respond(url: str) -> Response:
        if "crumbIssuer" in url:
            return Response(payload={"crumbRequestField": "Crumb", "crumb": "value"})
        return Response()

    session = Session(respond)
    client = client_with(session)

    client.create_username_password("id/one", "user<&", "pass<&", "run<&")
    client.delete_credential("id/one")

    xml = str(session.posts[0][1])
    assert "<id>id/one</id>" in xml
    assert "<username>user&lt;&amp;</username>" in xml
    assert "<password>pass&lt;&amp;</password>" in xml
    assert "<description>lol:run&lt;&amp;</description>" in xml
    assert "/credential/id%2Fone/doDelete" in session.posts[1][0]
