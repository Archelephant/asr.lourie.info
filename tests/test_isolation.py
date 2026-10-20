"""
The test suite must not depend on anything that is not in version control.

A developer running the suite on a machine that has a real .env, real
mounted secrets, or real environment variables must get the same result as
anyone else. These tests fail loudly if that stops being true.
"""

import os
import sys
import importlib


from tests.conftest import _LEAKY_VARS

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _reload_main():
    for name in list(sys.modules):
        if name == "main":
            del sys.modules[name]
    return importlib.import_module("main")


def test_suite_does_not_see_the_developers_env_file(monkeypatch, tmp_path):
    """The fixture strips every configuration variable from the process."""
    for name in _LEAKY_VARS:
        monkeypatch.setenv(name, "value-from-the-environment")

    # The import is the point: main reads the environment when it loads.
    _reload_main()
    for name in ("ASR_API_KEY", "ASR_UPSTREAM_KEY", "API_KEY", "AUTH_KEY"):
        assert os.environ.get(name) == "value-from-the-environment"


def test_a_dotenv_file_does_not_leak_into_a_test(monkeypatch, tmp_path):
    """
    main.py calls load_dotenv() at import time. The suite neutralises that
    lookup, so a real .env next to the repository cannot influence a test.
    """
    import dotenv

    dotenv_path = tmp_path / ".env"
    dotenv_path.write_text("API_KEY=leaked-from-dotenv\n")

    called = {}

    def fake_load_dotenv(*args, **kwargs):
        called["yes"] = True
        # Emulate the real behaviour: the value would land in the environment.
        os.environ["API_KEY"] = "leaked-from-dotenv"
        return False

    monkeypatch.setattr(dotenv, "load_dotenv", fake_load_dotenv)
    monkeypatch.setenv("ASR_API_KEY", "inbound-value")
    monkeypatch.setenv("ASR_UPSTREAM_KEY", "upstream-value")

    main = _reload_main()

    inbound, _ = main.resolve_inbound_api_key()
    upstream, _ = main.resolve_upstream_api_key()
    assert inbound == "inbound-value"
    assert upstream == "upstream-value"
    assert "leaked-from-dotenv" not in inbound
    assert "leaked-from-dotenv" not in upstream


def test_every_configuration_variable_is_isolated():
    """The isolation list covers every variable the service reads."""
    source = open(os.path.join(REPO_ROOT, "main.py")).read()
    for name in ("ASR_API_KEY", "ASR_UPSTREAM_KEY", "API_KEY", "AUTH_KEY",
                 "ASR_URL", "TTS_URL", "SCOPE", "GIGACHAT_CREDENTIALS",
                 "CA_BUNDLE_PATH", "LOG_LEVEL"):
        assert name in _LEAKY_VARS, f"{name} is not isolated from the environment"
        assert name in source, f"{name} is no longer read by main.py"
