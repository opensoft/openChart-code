"""Exercise docker/qa.sh against stand-in `docker` and `uv` programs.

Nothing here reaches a real Docker daemon: every run puts stubs first on PATH,
and the stubs only record their arguments and replay canned state. The
credentials are synthetic.
"""
from pathlib import Path
import os
import shlex
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
QA_SH = ROOT / "docker" / "qa.sh"
BASH = shutil.which("bash")

DOCKER_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" >> "$STUB_DIR/calls.log"
printf '%s\n' "${COMPOSE_PROJECT_NAME-<unset>}" >> "$STUB_DIR/project.log"
last=${!#}
case $1 in
  inspect)
    n=$(cat "$STUB_DIR/inspect.count" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$STUB_DIR/inspect.count"
    total=$(wc -l < "$STUB_DIR/states.txt")
    [ "$((n + 1))" -le "$total" ] || n=$((total - 1))
    sed -n "$((n + 1))p" "$STUB_DIR/states.txt"
    ;;
  logs)
    cat "$STUB_DIR/logs.$last" 2>/dev/null || true
    ;;
  compose)
    shift 3
    case $1 in
      ps)
        case "$*" in
          *"-q bench") [ -e "$STUB_DIR/no-bench" ] || echo bench ;;
          *"-q contracts-check") [ ! -e "$STUB_DIR/logs.check" ] || echo check ;;
          *) echo "SYN-PS-TABLE" ;;
        esac
        ;;
      up) exit "${STUB_UP_RC:-0}" ;;
      down) ;;
      exec)
        if [ -f "$STUB_DIR/credentials.env" ]; then
          cat "$STUB_DIR/credentials.env"
        else
          echo "cat: no such file" >&2
          exit 1
        fi
        ;;
      *) exit 64 ;;
    esac
    ;;
  *) exit 64 ;;
esac
"""

UV_STUB = r"""#!/usr/bin/env bash
printf '%s\n' "$*" > "$STUB_DIR/uv.args"
pwd > "$STUB_DIR/uv.cwd"
for name in QA_SITE QA_ADMIN_USER QA_ADMIN_PASSWORD QA_API_KEY QA_API_SECRET \
            QA_lower QA_EVIL CONTRACTS_DIR; do
  printf '%s=%s\n' "$name" "${!name-<unset>}"
done > "$STUB_DIR/uv.env"
exit "${STUB_UV_RC:-0}"
"""

HEALTHY = "running healthy 0 2026-01-01T00:00:00Z"
STARTING = "running starting 0 2026-01-01T00:00:00Z"
CREDENTIALS = {
    "QA_SITE": "qa.localhost",
    "QA_ADMIN_USER": "Administrator",
    "QA_ADMIN_PASSWORD": "SYN admin'pass $(false) \"q\"",
    "QA_API_KEY": "SYNKEY0123456789",
    "QA_API_SECRET": "SYNSECRET `x`; y&z",
}


@unittest.skipIf(BASH is None or os.name == "nt", "needs bash")
class QACommands(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name).resolve()
        self.stub = tmp / "stub"
        self.tmpdir = tmp / "tmpdir"
        self.contracts = tmp / "contracts"
        self.code = tmp / "assembly" / "code"
        for directory in (self.stub, self.tmpdir, self.contracts, self.code / "docker"):
            directory.mkdir(parents=True)
        for name, body in (("docker", DOCKER_STUB), ("uv", UV_STUB)):
            path = self.stub / name
            path.write_text(body)
            path.chmod(0o755)
        (self.contracts / "intake-census.yaml").write_text("SYN: census\n")
        self.script = self.code / "docker" / "qa.sh"
        shutil.copy(QA_SH, self.script)
        self.states([HEALTHY])

    def states(self, lines):
        (self.stub / "states.txt").write_text("\n".join(lines) + "\n")

    def credentials(self, values=CREDENTIALS, extra=""):
        text = "".join(f"{k}={shlex.quote(v)}\n" for k, v in values.items())
        (self.stub / "credentials.env").write_text(text + extra)

    def run_qa(self, *args, script=None, shell=(), cwd=None, **env):
        environment = {
            "PATH": f"{self.stub}:/usr/bin:/bin",
            "HOME": str(self.tmpdir),
            "TMPDIR": str(self.tmpdir),
            "STUB_DIR": str(self.stub),
            "QA_POLL_INTERVAL": "1",
            **env,
        }
        return subprocess.run(
            [BASH, *shell, str(script or self.script), *args],
            env=environment, cwd=cwd or self.tmpdir, capture_output=True,
            text=True, timeout=60,
        )

    def calls(self):
        path = self.stub / "calls.log"
        return path.read_text().splitlines() if path.exists() else []

    def uv_env(self):
        pairs = (line.split("=", 1) for line in (self.stub / "uv.env").read_text().splitlines())
        return dict(pairs)

    def compose_prefix(self):
        return f"compose -f {self.code / 'docker' / 'qa-compose.yaml'}"

    def test_script_is_executable_and_checked_in_shell_syntax(self):
        self.assertTrue(os.access(QA_SH, os.X_OK))
        subprocess.run([BASH, "-n", str(QA_SH)], check=True)

    def test_help_and_no_arguments_print_usage(self):
        for args in ((), ("help",), ("--help",)):
            result = self.run_qa(*args)
            self.assertEqual(result.returncode, 0)
            for word in ("up", "wait", "test", "down", "status"):
                self.assertIn(f"\n  {word} ", result.stdout)
        self.assertEqual(self.calls(), [])

    def test_unknown_command_and_extra_arguments_exit_two_without_docker(self):
        for args in (("bogus",), ("down", "-v"), ("down", "--volumes"), ("status", "-a")):
            result = self.run_qa(*args)
            self.assertEqual(result.returncode, 2, args)
            self.assertEqual(result.stdout, "")
            self.assertIn("Usage: qa.sh", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_down_never_removes_volumes(self):
        result = self.run_qa("down")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [f"{self.compose_prefix()} down"])

    def test_status_is_compose_ps(self):
        result = self.run_qa("status")
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "SYN-PS-TABLE\n")
        self.assertEqual(self.calls(), [f"{self.compose_prefix()} ps"])

    def test_paths_follow_the_script_not_the_directory_or_a_symlink(self):
        assembly = self.code.parent
        (assembly / "docker").symlink_to("code/docker")
        (assembly / "qa").symlink_to("code/docker/qa.sh")
        for script in (assembly / "docker" / "qa.sh", assembly / "qa"):
            for cwd in (assembly, self.tmpdir):
                (self.stub / "calls.log").unlink(missing_ok=True)
                result = self.run_qa("status", script=script, cwd=cwd)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls(), [f"{self.compose_prefix()} ps"], (script, cwd))

    def test_a_stray_project_name_does_not_redirect_the_stack(self):
        self.run_qa("status", COMPOSE_PROJECT_NAME="SYN-other-project")
        self.assertEqual((self.stub / "project.log").read_text(), "<unset>\n")

    def test_wait_returns_at_once_when_healthy(self):
        result = self.run_qa("wait")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("bench is healthy", result.stdout)

    def test_wait_polls_until_healthy(self):
        self.states([STARTING, STARTING, HEALTHY])
        result = self.run_qa("wait", QA_UP_TIMEOUT="30")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("waiting for the bench", result.stdout)

    def test_wait_times_out(self):
        self.states([STARTING])
        result = self.run_qa("wait", QA_UP_TIMEOUT="2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("timed out after 2s", result.stderr)

    def test_wait_rejects_a_bad_timeout(self):
        result = self.run_qa("wait", QA_UP_TIMEOUT="soon")
        self.assertEqual(result.returncode, 1)
        self.assertIn("QA_UP_TIMEOUT", result.stderr)

    def test_wait_fails_when_there_is_no_bench(self):
        (self.stub / "no-bench").touch()
        result = self.run_qa("wait")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no bench container", result.stderr)

    def test_wait_fails_fast_when_the_bench_exits_and_shows_the_evidence(self):
        self.states(["exited none 0 2026-01-01T00:00:00Z"])
        (self.stub / "logs.bench").write_text(
            "== OC-QA-STEP: bench init (frappe version-15)\n"
            "== OC-QA-STEP: migrate\n"
            "OC-QA-RESULT: FAIL at step 'migrate'\n"
        )
        result = self.run_qa("wait")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is exited", result.stderr)
        self.assertIn("== OC-QA-STEP: migrate", result.stderr)
        self.assertIn("OC-QA-RESULT: FAIL at step 'migrate'", result.stderr)

    def test_wait_fails_fast_when_the_bench_restarts(self):
        self.states([STARTING, "running starting 1 2026-01-01T00:01:00Z"])
        result = self.run_qa("wait", QA_UP_TIMEOUT="30")
        self.assertEqual(result.returncode, 1)
        self.assertIn("restarted", result.stderr)

    def test_wait_ignores_restarts_that_happened_before_it_began(self):
        self.states(["running starting 3 2026-01-01T00:00:00Z", "running healthy 3 2026-01-01T00:00:00Z"])
        self.assertEqual(self.run_qa("wait", QA_UP_TIMEOUT="30").returncode, 0)

    def test_wait_fails_fast_when_the_bench_logs_a_failure(self):
        self.states([STARTING])
        (self.stub / "logs.bench").write_text(
            "== OC-QA-STEP: site qa.localhost\nOC-QA-RESULT: FAIL at step 'site qa.localhost'\n"
        )
        result = self.run_qa("wait", QA_UP_TIMEOUT="30")
        self.assertEqual(result.returncode, 1)
        self.assertIn("logged OC-QA-RESULT: FAIL", result.stderr)
        self.assertIn("== OC-QA-STEP: site qa.localhost", result.stderr)

    def test_wait_fails_fast_when_the_bench_is_unhealthy(self):
        self.states(["running unhealthy 0 2026-01-01T00:00:00Z"])
        result = self.run_qa("wait")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unhealthy", result.stderr)

    def test_up_starts_the_stack_then_waits(self):
        result = self.run_qa("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls()[0], f"{self.compose_prefix()} up -d")
        self.assertTrue(any(call.startswith("inspect ") for call in self.calls()))
        self.assertFalse(any("down" in call for call in self.calls()))

    def test_up_reports_the_contracts_check_when_compose_fails(self):
        (self.stub / "logs.check").write_text("OC-QA-RESULT: FAIL at step 'contracts mount': none\n")
        result = self.run_qa("up", STUB_UP_RC="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("OC-QA-RESULT: FAIL at step 'contracts mount'", result.stderr)
        self.assertFalse(any(call.startswith("inspect ") for call in self.calls()))

    def test_test_requires_a_healthy_bench_and_reads_no_credentials(self):
        self.credentials()
        for state in (STARTING, "exited none 0 2026-01-01T00:00:00Z"):
            self.states([state])
            result = self.run_qa("test", OC_CONTRACTS_DIR=str(self.contracts))
            self.assertEqual(result.returncode, 1)
            self.assertIn("not healthy", result.stderr)
        self.assertFalse(any(" exec " in call for call in self.calls()))
        self.assertFalse((self.stub / "uv.args").exists())

    def test_test_exports_credentials_runs_the_runner_and_propagates_its_status(self):
        self.credentials(extra="OTHER=1\nQA_lower=1\nexport QA_EVIL=1\n")
        result = self.run_qa("test", OC_CONTRACTS_DIR=str(self.contracts), STUB_UV_RC="2")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual((self.stub / "uv.args").read_text(), "run scripts/run_p4_live.py\n")
        self.assertEqual((self.stub / "uv.cwd").read_text().strip(), str(self.code))
        seen = self.uv_env()
        for name, value in CREDENTIALS.items():
            self.assertEqual(seen[name], value)
        self.assertEqual(seen["CONTRACTS_DIR"], str(self.contracts))
        self.assertEqual(seen["QA_lower"], "<unset>")
        self.assertEqual(seen["QA_EVIL"], "<unset>")
        self.assertTrue(any(" exec -T bench cat /home/frappe/qa-sites/qa-credentials.env" in c for c in self.calls()))

    def test_test_never_prints_or_stores_credentials(self):
        self.credentials()
        for shell in ((), ("-x",)):
            result = self.run_qa("test", shell=shell, OC_CONTRACTS_DIR=str(self.contracts))
            self.assertEqual(result.returncode, 0, result.stderr)
            for value in CREDENTIALS.values():
                if value != "qa.localhost" and value != "Administrator":
                    self.assertNotIn(value, result.stdout + result.stderr, shell)
            self.assertNotIn("SYNKEY0123456789", result.stderr)
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    def test_test_leaves_an_explicit_contracts_dir_alone_and_resolves_relative_ones(self):
        self.credentials()
        elsewhere = self.contracts.parent / "elsewhere"
        self.run_qa("test", CONTRACTS_DIR=str(self.contracts), OC_CONTRACTS_DIR=str(elsewhere))
        self.assertEqual(self.uv_env()["CONTRACTS_DIR"], str(self.contracts))
        relative = os.path.relpath(self.contracts, self.code / "docker")
        self.run_qa("test", OC_CONTRACTS_DIR=relative)
        self.assertEqual(os.path.realpath(self.uv_env()["CONTRACTS_DIR"]), str(self.contracts))

    def test_test_names_the_missing_contracts_before_reading_credentials(self):
        self.credentials()
        result = self.run_qa("test")
        self.assertEqual(result.returncode, 1)
        self.assertIn("OC_CONTRACTS_DIR", result.stderr)
        self.assertFalse(any(" exec " in call for call in self.calls()))

    def test_test_fails_when_the_credentials_file_is_missing_or_incomplete(self):
        env = {"OC_CONTRACTS_DIR": str(self.contracts)}
        result = self.run_qa("test", **env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot read", result.stderr)
        self.credentials({"QA_SITE": "qa.localhost"})
        result = self.run_qa("test", **env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("lacks QA_API_KEY", result.stderr)
        self.assertFalse((self.stub / "uv.args").exists())

    def test_test_refuses_a_value_that_is_not_one_shlex_word_without_running_it(self):
        sentinel = self.tmpdir.parent / "SENTINEL"
        for value in (f"$(touch {sentinel})", "a;touch " + str(sentinel), "`touch %s`" % sentinel, "x y"):
            self.credentials(extra=f"QA_EVIL={value}\n")
            result = self.run_qa("test", OC_CONTRACTS_DIR=str(self.contracts))
            self.assertEqual(result.returncode, 1, value)
            self.assertIn("QA_EVIL", result.stderr)
            self.assertNotIn(value, result.stdout + result.stderr)
            self.assertFalse(sentinel.exists(), value)
            self.assertFalse((self.stub / "uv.args").exists())


if __name__ == "__main__":
    unittest.main()
