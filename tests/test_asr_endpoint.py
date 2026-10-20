"""
Tests for the speech-to-text endpoint.

The upstream service is always replaced by a stub, so the suite never
reaches the network and never spends anyone's quota.
"""


import pytest
import responses
from fastapi.testclient import TestClient

from tests.conftest import FAKE_ASR_URL, FAKE_INBOUND_KEY, FAKE_UPSTREAM_KEY

WAV_BYTES = b"RIFF\x00\x00\x00\x00WAVEfmt "


@pytest.fixture
def client(app_module):
    return TestClient(app_module.app, raise_server_exceptions=False)


def _auth():
    return {"X-API-Key": FAKE_INBOUND_KEY}


class TestSuccessfulTranscription:
    def test_returns_the_transcribed_text_and_language(self, client):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                FAKE_ASR_URL,
                json={"text": "Привет, мир", "language": "ru"},
                status=200,
            )
            response = client.post(
                "/asr",
                headers=_auth(),
                files={"file": ("a.wav", WAV_BYTES, "audio/wav")},
                data={"language": "ru-RU"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["text"] == "Привет, мир"
        assert body["language"] == "ru-RU"

    def test_an_empty_transcription_is_a_success(self, client):
        """An empty result is a valid answer, not an error."""
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, FAKE_ASR_URL, json={"text": ""}, status=200)
            response = client.post(
                "/asr",
                headers=_auth(),
                files={"file": ("a.wav", WAV_BYTES, "audio/wav")},
            )
        assert response.status_code == 200
        assert response.json()["text"] == ""


class TestUpstreamFailuresAreDistinguished:
    def test_a_rejected_payload_is_a_client_error(self, client):
        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                FAKE_ASR_URL,
                json={"detail": "Unsupported file extension: .bin"},
                status=400,
            )
            response = client.post(
                "/asr",
                headers=_auth(),
                files={"file": ("a.bin", b"x", "application/octet-stream")},
            )
        assert response.status_code == 400
        assert "Unsupported file extension" in response.json()["detail"]

    def test_a_rejected_credential_is_a_bad_gateway(self, client):
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, FAKE_ASR_URL, body="nope", status=401)
            response = client.post(
                "/asr",
                headers=_auth(),
                files={"file": ("a.wav", WAV_BYTES, "audio/wav")},
            )
        assert response.status_code == 502
        assert "credential" in response.json()["detail"].lower()

    def test_an_unreachable_upstream_is_unavailable(self, client):
        import requests

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                FAKE_ASR_URL,
                body=requests.exceptions.ConnectionError("refused"),
            )
            response = client.post(
                "/asr",
                headers=_auth(),
                files={"file": ("a.wav", WAV_BYTES, "audio/wav")},
            )
        assert response.status_code == 503

    def test_a_timeout_is_unavailable(self, client):
        import requests

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                FAKE_ASR_URL,
                body=requests.exceptions.ConnectTimeout("timed out"),
            )
            response = client.post(
                "/asr",
                headers=_auth(),
                files={"file": ("a.wav", WAV_BYTES, "audio/wav")},
            )
        assert response.status_code == 503


class TestTheMediaTypeReflectsTheUpload:
    def test_an_mp3_is_declared_as_mp3(self, client):
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, FAKE_ASR_URL, json={"text": ""}, status=200)
            client.post(
                "/asr",
                headers=_auth(),
                files={"file": ("a.mp3", b"ID3", "audio/mpeg")},
            )
            request = rsps.calls[0].request
        body = request.body
        if isinstance(body, bytes):
            body = body.decode("latin-1")
        assert "audio/mpeg" in body
        assert "audio/wav" not in body

    def test_an_unrecognised_extension_is_declared_as_binary(self, client):
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, FAKE_ASR_URL, json={"text": ""}, status=200)
            client.post(
                "/asr",
                headers=_auth(),
                files={"file": ("a.qqq", b"x", "application/octet-stream")},
            )
            request = rsps.calls[0].request
        body = request.body
        if isinstance(body, bytes):
            body = body.decode("latin-1")
        assert "application/octet-stream" in body

    def test_a_wav_is_declared_as_wav(self, client):
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, FAKE_ASR_URL, json={"text": ""}, status=200)
            client.post(
                "/asr",
                headers=_auth(),
                files={"file": ("a.wav", WAV_BYTES, "audio/wav")},
            )
            request = rsps.calls[0].request
        body = request.body
        if isinstance(body, bytes):
            body = body.decode("latin-1")
        assert "audio/wav" in body


class TestUpstreamCallsAreBounded:
    def test_a_transient_failure_is_retried(self, client):
        import requests

        with responses.RequestsMock() as rsps:
            rsps.add(
                responses.POST,
                FAKE_ASR_URL,
                body=requests.exceptions.ConnectionError("flaky"),
            )
            rsps.add(responses.POST, FAKE_ASR_URL, json={"text": "ok"}, status=200)
            response = client.post(
                "/asr",
                headers=_auth(),
                files={"file": ("a.wav", WAV_BYTES, "audio/wav")},
            )
        assert response.status_code == 200
        assert response.json()["text"] == "ok"

    def test_the_number_of_attempts_is_bounded(self, client):
        import requests

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            for _ in range(10):
                rsps.add(
                    responses.POST,
                    FAKE_ASR_URL,
                    body=requests.exceptions.ConnectionError("down"),
                )
            response = client.post(
                "/asr",
                headers=_auth(),
                files={"file": ("a.wav", WAV_BYTES, "audio/wav")},
            )
        assert response.status_code == 503
        # One layer of retries, not two nested ones.
        assert len(rsps.calls) <= 5, f"too many attempts: {len(rsps.calls)}"

    def test_a_client_error_is_not_retried(self, client):
        """A rejected payload will not become acceptable on a second try."""
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses.POST,
                FAKE_ASR_URL,
                json={"detail": "Unsupported file extension: .bin"},
                status=400,
            )
            response = client.post(
                "/asr",
                headers=_auth(),
                files={"file": ("a.bin", b"x", "application/octet-stream")},
            )
            attempts = len(rsps.calls)
        assert response.status_code == 400
        assert attempts == 1, f"a client error was retried {attempts} times"


class TestTheUpstreamReceivesTheOutboundCredential:
    def test_the_upstream_credential_is_presented(self, client):
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, FAKE_ASR_URL, json={"text": ""}, status=200)
            client.post(
                "/asr",
                headers=_auth(),
                files={"file": ("a.wav", WAV_BYTES, "audio/wav")},
            )
            request = rsps.calls[0].request
        assert request.headers["Authorization"] == f"Basic {FAKE_UPSTREAM_KEY}"

    def test_the_inbound_credential_is_not_presented_upstream(self, client):
        """The key a caller presents must never reach the upstream service."""
        with responses.RequestsMock() as rsps:
            rsps.add(responses.POST, FAKE_ASR_URL, json={"text": ""}, status=200)
            client.post(
                "/asr",
                headers=_auth(),
                files={"file": ("a.wav", WAV_BYTES, "audio/wav")},
            )
            request = rsps.calls[0].request
        assert FAKE_INBOUND_KEY not in request.headers.get("Authorization", "")


class TestTheEndpointDoesNotBlockTheEventLoop:
    def test_the_handler_is_a_worker(self, app_module):
        """
        The upstream call blocks. Declared as `async def` it would stall the
        event loop for every other request, so the handler must be a plain
        `def` that runs in the thread pool.
        """
        import inspect

        handler = app_module.speech_to_text
        assert not inspect.iscoroutinefunction(handler)
