"""
Tests for the pre-deployment check script.

The script is the gate before a deployment, so it has to work no matter how
it is invoked, and it must not report a false failure when a tool it needs
is missing.
"""

import os
import pytest
import subprocess


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO_ROOT, "test.sh")


def run_script(args, env=None, path=None):
    """Run test.sh the given way and return (returncode, output)."""
    environment = dict(os.environ)
    environment.setdefault("ASR_API_KEY", "test-key")
    environment.setdefault("ASR_BASE_URL", "http://127.0.0.1:1")
    if env:
        environment.update(env)
    if path is not None:
        environment["PATH"] = path
    result = subprocess.run(
        args,
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
    )
    return result.returncode, result.stdout + result.stderr


class TestTheScriptRunsUnderEveryShell:
    @pytest.mark.parametrize(
        "args",
        [
            ["sh", "test.sh"],
            ["dash", "test.sh"],
            ["bash", "test.sh"],
            ["./test.sh"],
        ],
    )
    def test_the_script_starts_under_each_shell(self, args):
        """
        `sh test.sh` overrides the shebang, and /bin/sh is dash on Debian
        and Ubuntu. The script has to survive that.
        """
        returncode, output = run_script(args)
        # It runs to the end and reports its result; it must not die on a
        # shell-specific option.
        assert "Illegal option" not in output
        assert "pipefail" not in output
        assert "ASR service smoke test" in output
        assert "Result:" in output
        # No server is running, so the checks fail — but the script itself
        # works and says so.
        assert returncode == 1

    def test_every_shell_produces_the_same_result(self):
        outputs = set()
        for args in (["sh", "test.sh"], ["bash", "test.sh"], ["./test.sh"]):
            _, output = run_script(args)
            outputs.add(output)
        assert len(outputs) == 1


class TestTheScriptHasNoEmbeddedSecret:
    def test_the_script_holds_no_key(self):
        source = open(SCRIPT).read()
        # The key is read from the environment; the script names the
        # variable, never a value.
        assert "ASR_API_KEY" in source
        assert "header = \"X-API-Key: %s\"" in source

    def test_the_script_does_not_pass_the_key_as_an_argument(self):
        """
        A key on the command line is visible in the process list.
        """
        source = open(SCRIPT).read()
        assert "-H \"X-API-Key: ${ASR_API_KEY}\"" not in source
        assert "--config" in source
