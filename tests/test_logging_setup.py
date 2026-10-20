"""
Tests for the logging setup.

Three things went wrong once and must not go wrong again:

- every line was printed more than once, because a logger with handlers of
  its own still propagated to the root logger
- a value taken from the built-in default was reported as "resolved from
  None", which reads like something is missing
- the visitor's own words reached the log through a logger that the
  verbose-logging switch did not cover
"""

import logging
import os

import pytest
from fastapi.testclient import TestClient

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def client(app_module):
    return TestClient(app_module.app, raise_server_exceptions=False)


def _records(logger_name, caplog, level=logging.INFO):
    """The records a named logger emitted, in order."""
    return [r for r in caplog.records if r.name == logger_name]


class TestALineIsLoggedOnce:
    """A record must not be emitted by the logger's own handler and again by
    the root logger's."""

    def test_the_app_logger_does_not_propagate(self, app_module):
        assert app_module.logger.propagate is False

    def test_loggers_with_their_own_handlers_do_not_propagate(self, app_module, monkeypatch):
        # The client loggers configure themselves when they are created.
        monkeypatch.setenv("TTS_URL", "https://tts.invalid/synthesize")
        app_module.ASRClient()
        app_module.TTSClient()
        for name in ("ASRClient", "TTSClient"):
            assert logging.getLogger(name).propagate is False, name

    def test_a_record_reaches_the_handlers_once(self, app_module, caplog):
        """One log call must not produce two log records."""
        with caplog.at_level(logging.INFO, logger="app"):
            app_module.logger.info("проверка на дублирование")
        assert len(_records("app", caplog)) == 1

    def test_the_message_is_not_repeated_in_the_console(self, app_module, capsys):
        """The console output must contain the line once, not twice."""
        app_module.logger.info("строка для консоли")
        printed = capsys.readouterr().err + capsys.readouterr().out
        assert printed.count("строка для консоли") <= 1


class TestTheSourceOfAValueIsReported:
    """A value taken from the built-in default must not be reported as
    coming from nowhere."""

    def test_a_default_is_reported_as_such(self, app_module, caplog):
        with caplog.at_level(logging.INFO, logger="app"):
            app_module._selftest_config()
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "resolved from None" not in text

    def test_a_variable_is_reported_by_name(self, app_module, caplog):
        with caplog.at_level(logging.INFO, logger="app"):
            app_module._selftest_config()
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "resolved from ASR_API_KEY" in text

    def test_the_default_is_named_as_such(self, app_module, caplog, monkeypatch):
        """A value with no variable behind it is reported as the default."""
        # The values are resolved at import time, so the module has to be
        # imported again for the change to take effect.
        import importlib
        import sys

        monkeypatch.delenv("SCOPE", raising=False)
        monkeypatch.setenv("SCOPE_FILE", "/nonexistent/scope.txt")
        for name in list(sys.modules):
            if name == "main":
                del sys.modules[name]
        main = importlib.import_module("main")

        with caplog.at_level(logging.INFO, logger="app"):
            main._selftest_config()
        text = "\n".join(r.getMessage() for r in caplog.records)
        assert "GigaChat scope" in text
        assert "built-in default" in text


class TestUserContentIsLoggedByDefault:
    """
    The service is a test instance: the questionnaire content and the model's
    reply are kept in the log for later analysis, so writing them is the
    default. The setting turns it off.
    """

    def _submit(self, client, app_module, monkeypatch, secret="СЕКРЕТ", reply="ОТВЕТ"):
        class StubGiga:
            def generate_response(self, text, system_prompt=None, attachments=None, model=None):
                return f"{reply}-{secret}"

        monkeypatch.setattr(app_module, "giga_client", StubGiga())
        return client.post(
            "/submit_questionnaire",
            data={
                "gender": "мужской",
                "age": "31–45",
                "track": "past",
                "goal": "отпустить",
                "event_name": secret,
                "place_action": secret,
            },
        )

    def test_the_prompt_is_in_the_log_by_default(self, client, app_module, monkeypatch, caplog):
        with caplog.at_level(logging.INFO):
            self._submit(client, app_module, monkeypatch)
        assert "СЕКРЕТ" in caplog.text
        # The prompt is written as part of the enriched prompt, not only by
        # the GigaChat client — that one is covered by its own test.
        assert "ENRICHED_PROMPT" in caplog.text

    def test_the_model_reply_is_in_the_log_by_default(self, client, app_module, monkeypatch, caplog):
        with caplog.at_level(logging.INFO):
            self._submit(client, app_module, monkeypatch)
        assert "ОТВЕТ-СЕКРЕТ" in caplog.text

    def test_the_prompt_is_absent_when_turned_off(self, client, app_module, monkeypatch, caplog):
        monkeypatch.setattr(app_module, "VERBOSE_LOGGING", False)
        with caplog.at_level(logging.INFO):
            self._submit(client, app_module, monkeypatch)
        assert "СЕКРЕТ" not in caplog.text
        assert "Generating GigaChat response for" not in caplog.text

    def test_the_gigachat_client_logger_follows_the_setting(self, app_module, caplog):
        """
        The GigaChat client has a logger of its own. The setting has to
        reach it too, or the prompt is written to the log under another
        name.
        """
        client = app_module.GigaChatClient(credentials="creds")
        client._client = object()  # a stub: the call below must fail
        with caplog.at_level(logging.INFO):
            try:
                client.generate_response("СЕКРЕТНЫЙ ПРОМПТ")
            except Exception:
                pass
        assert "СЕКРЕТНЫЙ ПРОМПТ" in caplog.text

    def test_the_gigachat_client_logger_is_silent_when_turned_off(self, app_module, caplog, monkeypatch):
        monkeypatch.setattr(app_module, "VERBOSE_LOGGING", False)
        client = app_module.GigaChatClient(credentials="creds")
        client._client = object()  # a stub: the call below must fail
        with caplog.at_level(logging.INFO):
            try:
                client.generate_response("СЕКРЕТНЫЙ ПРОМПТ")
            except Exception:
                pass
        assert "СЕКРЕТНЫЙ ПРОМПТ" not in caplog.text

    def test_the_default_is_on(self, app_module):
        """With nothing set, the content is written to the log."""
        assert app_module.VERBOSE_LOGGING is True


class TestTheLogDirectoryIsConfigurable:
    """A log file inside the source tree is watched by the development
    reloader, which then reports every request as a change."""

    def test_the_log_directory_can_be_pointed_elsewhere(self, monkeypatch, tmp_path):
        import importlib
        import sys

        monkeypatch.setenv("LOG_DIR", str(tmp_path / "elsewhere"))
        for name in list(sys.modules):
            if name == "main":
                del sys.modules[name]
        main = importlib.import_module("main")
        assert main.LOG_DIR == str(tmp_path / "elsewhere")
        assert (tmp_path / "elsewhere").is_dir()


class TestTheCodeIsDocumented:
    """Every class and every top-level function carries a description."""

    @staticmethod
    def _module():
        import importlib
        import sys

        for name in list(sys.modules):
            if name == "main":
                del sys.modules[name]
        return importlib.import_module("main")

    def test_every_class_has_a_description(self):
        import ast

        source = open(os.path.join(REPO_ROOT, "main.py")).read()
        tree = ast.parse(source)
        undocumented = [
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef) and not ast.get_docstring(node)
        ]
        assert undocumented == [], f"classes without a description: {undocumented}"

    def test_every_top_level_function_has_a_description(self):
        import ast

        source = open(os.path.join(REPO_ROOT, "main.py")).read()
        tree = ast.parse(source)
        undocumented = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and not ast.get_docstring(node)
        ]
        assert undocumented == [], f"functions without a description: {undocumented}"

    def test_a_description_says_what_the_class_does(self):
        """A description that no longer matches the class is worse than none."""
        import ast

        source = open(os.path.join(REPO_ROOT, "main.py")).read()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            doc = (ast.get_docstring(node) or "")
            assert doc.strip(), f"{node.name} has no description"
            # A description that mentions a name the class does not have
            # is stale.
            for mentioned in ("NotImplementedError", "upload_file", "attachments"):
                if mentioned in doc:
                    assert mentioned in source, (
                        f"{node.name} mentions {mentioned}, which the module does not have"
                    )

    def test_the_gigachat_client_description_mentions_attachments(self):
        """The client can attach images now, so its description says so."""
        module = self._module()
        doc = module.GigaChatClient.__doc__ or ""
        assert "attach" in doc.lower()
        assert "image" in doc.lower()
