"""
Tests for readiness reporting and the log-content policy.

Liveness reports that the process runs. Readiness reports whether the
dependencies can serve requests. Neither may disclose a credential, and
neither may write the visitor's own words into the log by default.
"""

import logging

import pytest
import responses
from fastapi.testclient import TestClient

from tests.conftest import FAKE_ASR_URL


@pytest.fixture
def client(app_module):
    return TestClient(app_module.app, raise_server_exceptions=False)


class TestLivenessAndReadinessAreSeparate:
    def test_liveness_reports_alive_while_a_dependency_is_down(self, client, app_module, monkeypatch):
        """Liveness must not depend on an external service."""
        import requests

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses.GET,
                FAKE_ASR_URL,
                body=requests.exceptions.ConnectionError("down"),
            )
            response = client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_readiness_reports_not_ready_while_a_dependency_is_down(self, client, app_module, monkeypatch):
        import requests

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses.GET,
                FAKE_ASR_URL,
                body=requests.exceptions.ConnectionError("down"),
            )
            response = client.get("/ready")
        assert response.status_code == 503
        body = response.json()
        assert body["status"] == "not_ready"
        assert "upstream_asr" in body["unavailable"]

    def test_readiness_names_the_unavailable_dependency(self, client, app_module, monkeypatch):
        import requests

        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(
                responses.GET,
                FAKE_ASR_URL,
                body=requests.exceptions.ConnectionError("down"),
            )
            body = client.get("/ready").json()
        assert body["checks"]["upstream_asr"]["ok"] is False
        assert body["checks"]["upstream_asr"]["detail"]

    def test_readiness_reports_ready_when_the_dependencies_work(self, client, app_module, monkeypatch):
        class StubGiga:
            def check_auth(self):
                return 0.01

        monkeypatch.setattr(app_module, "giga_client", StubGiga())
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses.GET, FAKE_ASR_URL, status=405)
            response = client.get("/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    def test_readiness_reports_a_rejected_credential(self, client, app_module, monkeypatch):
        class StubGiga:
            def check_auth(self):
                return 0.01

        monkeypatch.setattr(app_module, "giga_client", StubGiga())
        with responses.RequestsMock(assert_all_requests_are_fired=False) as rsps:
            rsps.add(responses.GET, FAKE_ASR_URL, status=401)
            body = client.get("/ready").json()
        assert body["checks"]["upstream_asr"]["ok"] is False
        assert "credential" in body["checks"]["upstream_asr"]["detail"].lower()


class TestUserContentIsLoggedByDefault:
    """
    The service runs as a test instance and keeps the questionnaire content
    and the model's reply in the log for later analysis, so writing it is
    the default. Turning the setting off keeps it out.
    """

    def _submit(self, client, app_module, monkeypatch, reply="generated"):
        class StubGiga:
            def generate_response(self, text, system_prompt=None, attachments=None, model=None):
                return reply

        monkeypatch.setattr(app_module, "giga_client", StubGiga())
        return client.post(
            "/submit_questionnaire",
            data={
                "gender": "мужской",
                "age": "31–45",
                "track": "past",
                "goal": "отпустить",
                "event_name": "СЕКРЕТНОЕВОСПОМИНАНИЕ",
                "place_action": "СЕКРЕТНОЕМЕСТО",
            },
        )

    def test_the_answers_are_in_the_log_by_default(self, client, app_module, monkeypatch, caplog):
        with caplog.at_level(logging.INFO):
            self._submit(client, app_module, monkeypatch)
        assert "СЕКРЕТНОЕВОСПОМИНАНИЕ" in caplog.text

    def test_the_model_response_is_in_the_log_by_default(self, client, app_module, monkeypatch, caplog):
        with caplog.at_level(logging.INFO):
            self._submit(client, app_module, monkeypatch, reply="СЕКРЕТНЫЙОТВЕТМОДЕЛИ")
        assert "СЕКРЕТНЫЙОТВЕТМОДЕЛИ" in caplog.text

    def test_the_answers_are_absent_when_turned_off(self, client, app_module, monkeypatch, caplog):
        monkeypatch.setattr(app_module, "VERBOSE_LOGGING", False)
        with caplog.at_level(logging.INFO):
            self._submit(client, app_module, monkeypatch)
        assert "СЕКРЕТНОЕВОСПОМИНАНИЕ" not in caplog.text
        assert "СЕКРЕТНОЕМЕСТО" not in caplog.text

    def test_the_model_response_is_absent_when_turned_off(self, client, app_module, monkeypatch, caplog):
        monkeypatch.setattr(app_module, "VERBOSE_LOGGING", False)
        with caplog.at_level(logging.INFO):
            self._submit(client, app_module, monkeypatch, reply="СЕКРЕТНЫЙОТВЕТМОДЕЛИ")
        assert "СЕКРЕТНЫЙОТВЕТМОДЕЛИ" not in caplog.text

    def test_operational_logging_is_unaffected(self, client, app_module, monkeypatch, caplog):
        with caplog.at_level(logging.INFO):
            self._submit(client, app_module, monkeypatch)
        assert "POST /submit_questionnaire" in caplog.text
        assert "status=200" in caplog.text

    def test_operational_logging_survives_turning_the_content_off(self, client, app_module, monkeypatch, caplog):
        monkeypatch.setattr(app_module, "VERBOSE_LOGGING", False)
        with caplog.at_level(logging.INFO):
            self._submit(client, app_module, monkeypatch)
        assert "POST /submit_questionnaire" in caplog.text


class TestCredentialsAreNeverLogged:
    def test_no_credential_appears_in_the_log(self, client, app_module, monkeypatch, caplog):
        from tests.conftest import FAKE_INBOUND_KEY, FAKE_UPSTREAM_KEY

        with caplog.at_level(logging.DEBUG):
            client.get("/ready")
        assert FAKE_INBOUND_KEY not in caplog.text
        assert FAKE_UPSTREAM_KEY not in caplog.text

    def test_the_startup_log_names_variables_without_values(self, app_module, caplog):
        with caplog.at_level(logging.INFO, logger="asr-api"):
            app_module._selftest_config()
        assert "ASR_API_KEY" in caplog.text
        assert app_module.INBOUND_API_KEY not in caplog.text
        assert app_module.UPSTREAM_API_KEY not in caplog.text
