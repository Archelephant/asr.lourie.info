"""
Shared test fixtures.

Every test runs against a clean, synthetic environment: no value from the
developer's real .env, from a mounted secret file, or from the surrounding
process environment can leak into a test. The upstream speech service and
GigaChat are always replaced by a stub, so the suite never touches the
network and never spends anyone's quota.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Configuration names used throughout the suite.
FAKE_INBOUND_KEY = "test-inbound-key"
FAKE_UPSTREAM_KEY = "test-upstream-key"
FAKE_ASR_URL = "https://asr.invalid/v1/audio/transcriptions"
FAKE_GIGACHAT_CREDENTIALS = "dGVzdC1jcmVkZW50aWFscw=="
FAKE_SCOPE = "GIGACHAT_API_PERS"

# Environment variables that must not leak into a test.
_LEAKY_VARS = (
    "ASR_API_KEY",
    "ASR_API_KEY_FILE",
    "ASR_UPSTREAM_KEY",
    "ASR_UPSTREAM_KEY_FILE",
    "API_KEY",
    "API_KEY_FILE",
    "AUTH_KEY",
    "AUTH_KEY_FILE",
    "ASR_URL",
    "ASR_URL_FILE",
    "TTS_URL",
    "TTS_URL_FILE",
    "SCOPE",
    "SCOPE_FILE",
    "GIGACHAT_CREDENTIALS",
    "GIGACHAT_CREDENTIALS_FILE",
    "CA_BUNDLE_PATH",
    "LOG_LEVEL",
)


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch, tmp_path):
    """
    Strip every configuration variable from the environment and point the
    file-mounted forms at a directory that holds no secrets.

    The working directory is deliberately left alone: main.py mounts
    `frontend/static` by a relative path, so the repository root has to stay
    the working directory for the module to import.

    main.py calls load_dotenv() at import time, which would pull the
    developer's real .env into the environment and let it override these
    settings. The .env lookup is neutralised for the duration of the test so
    the suite never depends on a file that is not in version control.
    """
    for name in _LEAKY_VARS:
        monkeypatch.delenv(name, raising=False)

    empty_dir = tmp_path / "empty-secrets"
    empty_dir.mkdir()
    for name in _LEAKY_VARS:
        if name.endswith("_FILE"):
            monkeypatch.setenv(name, str(empty_dir / "absent"))

    import dotenv

    monkeypatch.setattr(dotenv, "load_dotenv", lambda *a, **k: False)

    yield


@pytest.fixture
def env_with_keys(monkeypatch, isolated_environment):
    """A configured environment with the two key roles set apart."""
    monkeypatch.setenv("ASR_API_KEY", FAKE_INBOUND_KEY)
    monkeypatch.setenv("ASR_UPSTREAM_KEY", FAKE_UPSTREAM_KEY)
    monkeypatch.setenv("ASR_URL", FAKE_ASR_URL)
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", FAKE_GIGACHAT_CREDENTIALS)
    monkeypatch.setenv("SCOPE", FAKE_SCOPE)
    return monkeypatch


@pytest.fixture
def app_module(monkeypatch, env_with_keys):
    """main imported with a clean, configured environment."""
    for name in list(sys.modules):
        if name == "main":
            del sys.modules[name]
    import main  # noqa: E402  (import must follow the environment setup)
    return main
