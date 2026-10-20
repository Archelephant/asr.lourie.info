"""
Tests for inbound API authentication.

The rule under test: the key protects the endpoints that forward a request
onward to another service. Endpoints that answer locally stay open, so the
browser demo keeps working.
"""

import importlib
import sys

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient


def reload_main():
    for name in list(sys.modules):
        if name == "main":
            del sys.modules[name]
    return importlib.import_module("main")


def _protected_paths(app_module):
    """The paths whose routes carry an authentication dependency."""
    return {
        route.path
        for route in app_module.app.routes
        if isinstance(route, APIRoute) and route.dependencies
    }


@pytest.fixture
def client(app_module):
    return TestClient(app_module.app, raise_server_exceptions=False)


class TestForwardingEndpointsRequireAKey:
    """A forwarding endpoint rejects a request without a valid key."""

    def test_missing_header_is_rejected(self, client):
        response = client.post("/asr", files={"file": ("a.wav", b"RIFF", "audio/wav")})
        assert response.status_code == 401

    def test_wrong_key_is_rejected(self, client):
        response = client.post(
            "/asr",
            headers={"X-API-Key": "not-the-configured-key"},
            files={"file": ("a.wav", b"RIFF", "audio/wav")},
        )
        assert response.status_code == 401

    def test_tts_requires_a_key_too(self, client):
        response = client.post("/tts", data={"text": "hello"})
        assert response.status_code == 401

    def test_correct_key_is_accepted(self, client, app_module, monkeypatch):
        """A request with the right key reaches the handler."""
        monkeypatch.setattr(
            app_module, "get_asr_client",
            lambda: _StubASRClient(),
        )
        response = client.post(
            "/asr",
            headers={"X-API-Key": app_module.INBOUND_API_KEY},
            files={"file": ("a.wav", b"RIFF", "audio/wav")},
        )
        assert response.status_code == 200
        assert response.json()["text"] == "transcribed"


class _StubASRClient:
    """Stands in for the upstream speech client."""

    def recognize_audio(self, file, filename, language, model):
        return "transcribed"

    def close(self):
        pass


class TestAuthenticationHappensBeforeAnyUpstreamWork:
    """An unauthenticated request must not reach an external service."""

    def test_no_upstream_call_without_a_key(self, app_module, monkeypatch):
        calls = []

        class Tripwire:
            def recognize_audio(self, *args, **kwargs):
                calls.append(args)
                return "should not happen"

        monkeypatch.setattr(app_module, "get_asr_client", lambda: Tripwire())
        client = TestClient(app_module.app, raise_server_exceptions=False)

        response = client.post("/asr", files={"file": ("a.wav", b"RIFF", "audio/wav")})
        assert response.status_code == 401
        assert calls == [], "the upstream service was called for an unauthenticated request"


class TestLocalEndpointsStayOpen:
    """The browser demo must keep working without a key."""

    @pytest.mark.parametrize("path", ["/health", "/", "/static/script.js"])
    def test_local_endpoints_answer_without_a_key(self, client, path):
        response = client.get(path)
        assert response.status_code == 200

    def test_readiness_answers_without_a_key(self, client):
        """Readiness is a monitoring endpoint: it answers without a key."""
        response = client.get("/ready")
        # It reports 503 while a dependency is unusable, but it answers.
        assert response.status_code in (200, 503)
        assert response.json()["status"] in ("ready", "not_ready")

    def test_questionnaire_submission_is_accepted_without_a_key(self, client, app_module, monkeypatch):
        class StubGiga:
            def generate_response(self, text, system_prompt=None, attachments=None, model=None):
                return "generated"

        monkeypatch.setattr(app_module, "giga_client", StubGiga())
        response = client.post(
            "/submit_questionnaire",
            data={
                "gender": "test", "age": "30", "track": "past", "goal": "отпустить",
                "event_name": "test", "age_at_moment": "10",
                "place_action": "test", "light_weather": "солнце",
            },
        )
        assert response.status_code == 200
        assert response.json()["response"] == "generated"


class TestMissingConfigurationIsNotAnOpenDoor:
    """A deployment with no key must not fall open."""

    def test_no_configured_key_yields_503(self, monkeypatch):
        for name in ("ASR_API_KEY", "API_KEY", "AUTH_KEY"):
            monkeypatch.delenv(name, raising=False)
        main = reload_main()
        client = TestClient(main.app, raise_server_exceptions=False)

        response = client.post("/asr", files={"file": ("a.wav", b"RIFF", "audio/wav")})
        assert response.status_code == 503
        assert "API key" in response.json()["detail"]


class TestTheKeyIsNeverDisclosed:
    """Neither the presented nor the configured key may appear in output."""

    def test_response_body_does_not_contain_the_key(self, client, app_module):
        response = client.post(
            "/asr",
            headers={"X-API-Key": "wrong-key-value"},
            files={"file": ("a.wav", b"RIFF", "audio/wav")},
        )
        assert app_module.INBOUND_API_KEY not in response.text
        assert "wrong-key-value" not in response.text

    def test_log_does_not_contain_the_key(self, client, app_module, caplog):
        import logging

        with caplog.at_level(logging.DEBUG):
            client.post(
                "/asr",
                headers={"X-API-Key": "wrong-key-value"},
                files={"file": ("a.wav", b"RIFF", "audio/wav")},
            )
        assert app_module.INBOUND_API_KEY not in caplog.text
        assert "wrong-key-value" not in caplog.text

    def test_browser_content_does_not_contain_the_key(self, client, app_module):
        for path in ("/", "/static/script.js"):
            response = client.get(path)
            assert app_module.INBOUND_API_KEY not in response.text


class TestTheRuleCoversEveryForwardingEndpoint:
    """
    The rule is "every endpoint that forwards a request onward". A new
    forwarding endpoint that is not protected has to break this test, rather
    than slipping through unnoticed.
    """

    def test_every_forwarding_endpoint_is_protected(self, app_module):
        protected = _protected_paths(app_module)
        assert "/asr" in protected
        assert "/tts" in protected

    def test_a_new_unprotected_forwarding_endpoint_is_detected(self, app_module):
        """The check enumerates the routes itself, so it cannot go stale."""
        forwarding = {
            "/asr",
            "/tts",
            "/submit_questionnaire",
        }
        protected = _protected_paths(app_module)
        # The questionnaire is the documented exception: a browser cannot
        # hold a secret. Every other forwarding endpoint must be protected.
        unprotected_forwarding = forwarding - protected - {"/submit_questionnaire"}
        assert unprotected_forwarding == set()
