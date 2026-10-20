"""
Tests for credential resolution.

Covers the documented order of environment variable names, the precedence of
the file-mounted form, the separation of the two key roles, and the rule that
a resolved *name* may be logged while the *value* may not.
"""

import pytest
import sys
import importlib


def reload_main(monkeypatch, tmp_path):
    """Import main fresh under the current (monkeypatched) environment."""
    for name in list(sys.modules):
        if name == "main":
            del sys.modules[name]
    return importlib.import_module("main")


class TestResolveConfigOrder:
    """Each name in the list is tried in the documented order."""

    def test_first_name_wins(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FIRST", "one")
        monkeypatch.setenv("SECOND", "two")
        main = reload_main(monkeypatch, tmp_path)
        value, name = main.resolve_config(("FIRST", "SECOND"))
        assert value == "one"
        assert name == "FIRST"

    def test_falls_through_to_later_name(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SECOND", "two")
        main = reload_main(monkeypatch, tmp_path)
        value, name = main.resolve_config(("FIRST", "SECOND"))
        assert value == "two"
        assert name == "SECOND"

    def test_absent_returns_default_and_no_name(self, monkeypatch, tmp_path):
        main = reload_main(monkeypatch, tmp_path)
        value, name = main.resolve_config(("ABSENT_ONE", "ABSENT_TWO"), default="fallback")
        assert value == "fallback"
        assert name is None

    def test_required_raises_and_names_the_candidates(self, monkeypatch, tmp_path):
        main = reload_main(monkeypatch, tmp_path)
        with pytest.raises(ValueError) as exc:
            main.resolve_config(("ABSENT_ONE", "ABSENT_TWO"), required=True)
        message = str(exc.value)
        assert "ABSENT_ONE" in message
        assert "ABSENT_TWO" in message

    def test_empty_value_is_treated_as_absent(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FIRST", "")
        monkeypatch.setenv("SECOND", "two")
        main = reload_main(monkeypatch, tmp_path)
        value, name = main.resolve_config(("FIRST", "SECOND"))
        assert value == "two"
        assert name == "SECOND"


class TestFileMountedForm:
    """The file-mounted form wins over the plain environment variable."""

    def test_file_wins_over_plain_variable(self, monkeypatch, tmp_path):
        secret = tmp_path / "key.txt"
        secret.write_text("value-from-file\n")
        monkeypatch.setenv("SOME_KEY", "value-from-env")
        monkeypatch.setenv("SOME_KEY_FILE", str(secret))
        main = reload_main(monkeypatch, tmp_path)
        value, name = main.resolve_config(("SOME_KEY",))
        assert value == "value-from-file"

    def test_file_value_is_stripped(self, monkeypatch, tmp_path):
        secret = tmp_path / "key.txt"
        secret.write_text("  padded-value  \n")
        monkeypatch.setenv("SOME_KEY_FILE", str(secret))
        main = reload_main(monkeypatch, tmp_path)
        value, _ = main.resolve_config(("SOME_KEY",))
        assert value == "padded-value"

    def test_missing_file_falls_back_to_plain_variable(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SOME_KEY", "value-from-env")
        monkeypatch.setenv("SOME_KEY_FILE", str(tmp_path / "absent.txt"))
        main = reload_main(monkeypatch, tmp_path)
        value, _ = main.resolve_config(("SOME_KEY",))
        assert value == "value-from-env"


class TestKeyRolesAreSeparate:
    """The inbound key and the upstream key are distinct roles."""

    def test_distinct_values_are_resolved_separately(self, monkeypatch, tmp_path):
        monkeypatch.setenv("ASR_API_KEY", "inbound-value")
        monkeypatch.setenv("ASR_UPSTREAM_KEY", "upstream-value")
        main = reload_main(monkeypatch, tmp_path)
        inbound, inbound_name = main.resolve_inbound_api_key()
        upstream, upstream_name = main.resolve_upstream_api_key()
        assert inbound == "inbound-value"
        assert upstream == "upstream-value"
        assert inbound != upstream
        assert inbound_name == "ASR_API_KEY"
        assert upstream_name == "ASR_UPSTREAM_KEY"

    def test_historical_name_serves_both_roles(self, monkeypatch, tmp_path):
        """A deployment written before the rework keeps working unchanged."""
        monkeypatch.setenv("API_KEY", "shared-value")
        main = reload_main(monkeypatch, tmp_path)
        inbound, _ = main.resolve_inbound_api_key()
        upstream, _ = main.resolve_upstream_api_key()
        assert inbound == "shared-value"
        assert upstream == "shared-value"

    def test_inbound_key_is_not_used_upstream(self, monkeypatch, tmp_path):
        """The value a caller presents must never reach the upstream service."""
        monkeypatch.setenv("ASR_API_KEY", "inbound-only")
        monkeypatch.setenv("ASR_UPSTREAM_KEY", "upstream-only")
        main = reload_main(monkeypatch, tmp_path)
        upstream, _ = main.resolve_upstream_api_key()
        assert upstream == "upstream-only"
        assert "inbound-only" not in upstream

    def test_auth_key_is_the_last_resort_for_both_roles(self, monkeypatch, tmp_path):
        monkeypatch.setenv("AUTH_KEY", "last-resort")
        main = reload_main(monkeypatch, tmp_path)
        assert main.resolve_inbound_api_key()[0] == "last-resort"
        assert main.resolve_upstream_api_key()[0] == "last-resort"


class TestResolvedNameIsSafeToLog:
    """A resolved name may be logged; the value may not."""

    def test_resolution_reports_the_name_not_the_value(self, monkeypatch, tmp_path):
        monkeypatch.setenv("SOME_KEY", "super-secret-value")
        main = reload_main(monkeypatch, tmp_path)
        value, name = main.resolve_config(("SOME_KEY",))
        assert name == "SOME_KEY"
        assert "super-secret-value" not in name

    def test_startup_log_names_variables_without_values(self, monkeypatch, tmp_path, caplog):
        import logging

        monkeypatch.setenv("ASR_API_KEY", "super-secret-inbound")
        monkeypatch.setenv("ASR_UPSTREAM_KEY", "super-secret-upstream")
        monkeypatch.setenv("GIGACHAT_CREDENTIALS", "super-secret-gigachat")
        main = reload_main(monkeypatch, tmp_path)

        with caplog.at_level(logging.INFO, logger="asr-api"):
            main._selftest_config()

        text = caplog.text
        assert "ASR_API_KEY" in text
        assert "super-secret-inbound" not in text
        assert "super-secret-upstream" not in text
        assert "super-secret-gigachat" not in text
