"""Exercise docker/qa.sh against stand-in `docker` and `uv` programs.

Nothing here reaches a real Docker daemon: every run puts stubs first on PATH,
and the stubs only record their arguments and replay canned state. The
credentials are synthetic.
"""
from pathlib import Path
import os
import random
import re
import shlex
import shutil
import string
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
QA_SH = ROOT / "docker" / "qa.sh"
COMPOSE_YAML = ROOT / "docker" / "qa-compose.yaml"
BASH = shutil.which("bash")

# `docker compose -p <project> -f <file> <verb> ...` is `compose -p ... ` here.
# Log lines are "<ISO time> <text>" and `docker logs` honours --since and
# --tail, so a script that drops either one reads the wrong lines.
DOCKER_STUB = r"""#!/usr/bin/env bash
export LC_ALL=C
printf '%s\n' "$*" >> "$STUB_DIR/calls.log"
case $1 in
  inspect)
    case "$*" in
      *working_dir*) cat "$STUB_DIR/workdir"; exit 0 ;;
    esac
    n=$(cat "$STUB_DIR/inspect.count" 2>/dev/null || echo 0)
    echo $((n + 1)) > "$STUB_DIR/inspect.count"
    total=$(wc -l < "$STUB_DIR/states.txt")
    [ "$((n + 1))" -le "$total" ] || n=$((total - 1))
    sed -n "$((n + 1))p" "$STUB_DIR/states.txt"
    ;;
  logs)
    shift
    since=''
    tail=''
    while [ "$#" -gt 1 ]; do
      case $1 in
        --since) since=$2; shift 2 ;;
        --tail) tail=$2; shift 2 ;;
        *) shift ;;
      esac
    done
    [ -f "$STUB_DIR/logs.$1" ] || exit 0
    out=$(while IFS= read -r line; do
      stamp=${line%% *}
      if [ -z "$since" ] || [[ $stamp > $since ]] || [[ $stamp == "$since" ]]; then
        printf '%s\n' "${line#* }"
      fi
    done < "$STUB_DIR/logs.$1")
    if [ -n "$tail" ]; then printf '%s\n' "$out" | tail -n "$tail"; else printf '%s\n' "$out"; fi
    ;;
  compose)
    [ "$2" = -p ] && [ "$3" = open-chart-qa ] && [ "$4" = -f ] || exit 65
    shift 5
    case $1 in
      ps)
        case "$*" in
          "ps -a -q bench") [ -e "$STUB_DIR/no-bench" ] || echo bench ;;
          "ps -a -q contracts-check") [ ! -e "$STUB_DIR/logs.check" ] || echo check ;;
          "ps -a -q") [ -e "$STUB_DIR/no-bench" ] || echo bench ;;
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
for name in QA_SITE QA_ADMIN_USER QA_ADMIN_PASSWORD QA_DB_ROOT_PASSWORD \
            QA_API_KEY QA_API_SECRET QA_lower QA_EVIL CONTRACTS_DIR \
            OC_BASE OC_API_KEY OC_API_SECRET OC_SITE; do
  printf '%s=%s\n' "$name" "${!name-<unset>}"
done > "$STUB_DIR/uv.env"
exit "${STUB_UV_RC:-0}"
"""

T0 = "2026-01-01T00:00:00Z"
HEALTHY = f"running healthy 0 {T0}"
STARTING = f"running starting 0 {T0}"
EXITED = f"exited none 0 {T0}"
# The key is a bare word under shlex.quote, underscores and all.
CREDENTIALS = {
    "QA_SITE": "qa.localhost",
    "QA_ADMIN_USER": "Administrator",
    "QA_ADMIN_PASSWORD": "SYN admin'pass $(false) \"q\"",
    "QA_API_KEY": "SYN_key_0123456789",
    "QA_API_SECRET": "SYN_secret `x`; y&z",
}
RUNNER_VALUES = ("SYN_key_0123456789", "SYN_secret `x`; y&z")


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
        self.workdir(self.code / "docker")

    def states(self, lines):
        (self.stub / "states.txt").write_text("\n".join(lines) + "\n")

    def workdir(self, path):
        (self.stub / "workdir").write_text(f"{path}\n")

    def logs(self, name, lines):
        (self.stub / f"logs.{name}").write_text("".join(f"{t} {text}\n" for t, text in lines))

    def credentials(self, values=CREDENTIALS, extra=""):
        text = "".join(f"{k}={shlex.quote(v)}\n" for k, v in values.items())
        (self.stub / "credentials.env").write_text(text + extra)

    def forget_runs(self):
        for name in ("uv.args", "uv.cwd", "uv.env", "calls.log"):
            (self.stub / name).unlink(missing_ok=True)

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
        return f"compose -p open-chart-qa -f {self.code / 'docker' / 'qa-compose.yaml'}"

    def compose_calls(self):
        return [call for call in self.calls() if call.startswith("compose ")]

    # ---- shape of the script ------------------------------------------------

    def test_script_is_executable_and_checked_in_shell_syntax(self):
        self.assertTrue(os.access(QA_SH, os.X_OK))
        subprocess.run([BASH, "-n", str(QA_SH)], check=True)

    def test_project_name_is_the_one_the_compose_file_declares(self):
        declared = re.search(r"^name:\s*(\S+)", COMPOSE_YAML.read_text(), re.M).group(1)
        self.assertEqual(declared, "open-chart-qa")
        self.assertIn(f"QA_PROJECT={declared}\n", QA_SH.read_text())

    def test_decode_word_round_trips_shlex_quote(self):
        source = re.search(r"^decode_word\(\) \{.*?^\}", QA_SH.read_text(), re.S | re.M).group(0)
        cases = ["oc_qa_admin", "Secret_2026", "a_b", "_", "plain", "", "it's", "a b", "$(x)",
                 "x'y'z", "ünï", "tab\tx", "back\\slash", '"dq"', "'", "''", '"']
        alphabet = string.ascii_letters + string.digits + "_@%+=:,./-'\"$`\\ !*?;&|<>(){}[]#~^"
        generator = random.Random(1)
        cases += ["".join(generator.choice(alphabet) for _ in range(generator.randint(0, 12)))
                  for _ in range(500)]
        driver = (
            source + '\nwhile IFS= read -r line; do\n'
            '  if decode_word "$line"; then printf "OK:%s\\n" "$DECODED"; else echo REFUSED; fi\n'
            "done\n"
        )
        result = subprocess.run([BASH, "-c", driver], input="".join(shlex.quote(c) + "\n" for c in cases),
                                capture_output=True, text=True, check=True)
        self.assertEqual(result.stdout.splitlines(), ["OK:" + case for case in cases])
        refused = subprocess.run([BASH, "-c", driver], input="'abc\n$(x)\na b\n\"x\n", capture_output=True,
                                 text=True, check=True)
        self.assertEqual(refused.stdout.splitlines(), ["REFUSED"] * 4)

    # ---- help, usage errors ---------------------------------------------------

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

    # ---- compose invocations --------------------------------------------------

    def test_down_never_removes_volumes(self):
        result = self.run_qa("down")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.compose_calls()[-1], f"{self.compose_prefix()} down")
        self.assertFalse(any("-v" in call.split() or "--volumes" in call for call in self.calls()))

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
                self.forget_runs()
                result = self.run_qa("status", script=script, cwd=cwd)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls(), [f"{self.compose_prefix()} ps"], (script, cwd))

    def test_a_relative_script_path_works_even_with_cdpath_set(self):
        decoy = self.tmpdir.parent / "decoy"
        (decoy / "docker").mkdir(parents=True)
        for cdpath in ("", str(decoy), f"{decoy}:."):
            self.forget_runs()
            result = self.run_qa("status", script="docker/qa.sh", cwd=self.code, CDPATH=cdpath)
            self.assertEqual(result.returncode, 0, (cdpath, result.stderr))
            self.assertEqual(result.stdout, "SYN-PS-TABLE\n", cdpath)
            self.assertEqual(self.calls(), [f"{self.compose_prefix()} ps"], cdpath)

    def test_every_compose_call_names_the_project_whatever_the_environment_says(self):
        env = {"COMPOSE_PROJECT_NAME": "SYN-other-project", "COMPOSE_FILE": "/SYN/other.yaml"}
        for command in ("status", "down", "wait", "up"):
            self.forget_runs()
            result = self.run_qa(command, **env)
            self.assertEqual(result.returncode, 0, (command, result.stderr))
            self.assertTrue(self.compose_calls(), command)
            for call in self.compose_calls():
                self.assertTrue(call.startswith(self.compose_prefix() + " "), call)

    # ---- wait -----------------------------------------------------------------

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

    def test_wait_rejects_bad_numbers(self):
        for name, value in (("QA_UP_TIMEOUT", "soon"), ("QA_POLL_INTERVAL", "0"), ("QA_CREATED_GRACE", "-1")):
            result = self.run_qa("wait", **{name: value})
            self.assertEqual(result.returncode, 1, name)
            self.assertIn(name, result.stderr)

    def test_wait_fails_when_there_is_no_bench(self):
        (self.stub / "no-bench").touch()
        result = self.run_qa("wait")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no bench container", result.stderr)

    def test_wait_fails_fast_when_the_bench_exits_and_shows_the_evidence(self):
        self.states([EXITED])
        self.logs("bench", [
            (T0, "== OC-QA-STEP: bench init (frappe version-15)"),
            (T0, "== OC-QA-STEP: migrate"),
            (T0, "OC-QA-RESULT: FAIL at step 'migrate'"),
        ])
        result = self.run_qa("wait")
        self.assertEqual(result.returncode, 1)
        self.assertIn("is exited", result.stderr)
        self.assertIn("== OC-QA-STEP: migrate", result.stderr)
        self.assertIn("OC-QA-RESULT: FAIL at step 'migrate'", result.stderr)

    def test_the_evidence_leaves_out_results_from_before_the_wait_began(self):
        self.states([EXITED])
        self.logs("bench", [
            ("2025-12-31T00:00:00Z", "== OC-QA-STEP: STALE-STEP"),
            ("2025-12-31T00:00:00Z", "OC-QA-RESULT: READY (stale run)"),
            (T0, "== OC-QA-STEP: migrate"),
            (T0, "OC-QA-RESULT: FAIL at step 'migrate'"),
        ])
        result = self.run_qa("wait")
        self.assertEqual(result.returncode, 1)
        self.assertIn("OC-QA-RESULT: FAIL at step 'migrate'", result.stderr)
        self.assertNotIn("STALE", result.stderr)
        self.assertNotIn("READY", result.stderr)

    def test_wait_fails_fast_when_the_bench_restarts(self):
        self.states([STARTING, "running starting 1 2026-01-01T00:01:00Z"])
        result = self.run_qa("wait", QA_UP_TIMEOUT="30")
        self.assertEqual(result.returncode, 1)
        self.assertIn("restarted", result.stderr)

    def test_wait_ignores_restarts_that_happened_before_it_began(self):
        self.states([f"running starting 3 {T0}", f"running healthy 3 {T0}"])
        self.assertEqual(self.run_qa("wait", QA_UP_TIMEOUT="30").returncode, 0)

    def test_wait_fails_fast_when_the_bench_logs_a_failure(self):
        self.states([STARTING])
        self.logs("bench", [
            (T0, "== OC-QA-STEP: site qa.localhost"),
            (T0, "OC-QA-RESULT: FAIL at step 'site qa.localhost'"),
        ])
        result = self.run_qa("wait", QA_UP_TIMEOUT="30")
        self.assertEqual(result.returncode, 1)
        self.assertIn("logged OC-QA-RESULT: FAIL", result.stderr)
        self.assertIn("== OC-QA-STEP: site qa.localhost", result.stderr)

    def test_wait_ignores_a_failure_logged_by_an_earlier_run_of_the_container(self):
        self.states([STARTING, HEALTHY])
        self.logs("bench", [("2025-12-31T00:00:00Z", "OC-QA-RESULT: FAIL at step 'earlier run'")])
        result = self.run_qa("wait", QA_UP_TIMEOUT="30")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_wait_fails_fast_when_the_bench_is_unhealthy(self):
        self.states([f"running unhealthy 0 {T0}"])
        result = self.run_qa("wait")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unhealthy", result.stderr)

    def test_wait_fails_fast_when_the_bench_is_paused(self):
        self.states([f"paused healthy 0 {T0}"])
        result = self.run_qa("wait", QA_UP_TIMEOUT="600")
        self.assertEqual(result.returncode, 1)
        self.assertIn("the bench container is paused", result.stderr)
        self.assertNotIn("timed out", result.stderr)

    def test_wait_gives_up_on_a_bench_that_stays_created(self):
        self.states(["created none 0 0001-01-01T00:00:00Z"])
        result = self.run_qa("wait", QA_UP_TIMEOUT="60", QA_CREATED_GRACE="2")
        self.assertEqual(result.returncode, 1)
        self.assertIn("created but has not started after 2s", result.stderr)

    def test_wait_allows_a_created_bench_that_starts_within_the_grace(self):
        self.states(["created none 0 0001-01-01T00:00:00Z", STARTING, HEALTHY])
        result = self.run_qa("wait", QA_UP_TIMEOUT="60", QA_CREATED_GRACE="30")
        self.assertEqual(result.returncode, 0, result.stderr)

    # ---- up -------------------------------------------------------------------

    def test_up_starts_the_stack_then_waits(self):
        result = self.run_qa("up")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{self.compose_prefix()} up -d", self.calls())
        self.assertTrue(any(call.startswith("inspect ") for call in self.calls()))
        self.assertFalse(any(call.endswith(" down") for call in self.calls()))

    def test_up_reports_the_contracts_check_when_compose_fails(self):
        self.logs("check", [(T0, "OC-QA-RESULT: FAIL at step 'contracts mount': none")])
        result = self.run_qa("up", STUB_UP_RC="1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("OC-QA-RESULT: FAIL at step 'contracts mount'", result.stderr)
        polled = [c for c in self.calls() if c.startswith("inspect ") and "working_dir" not in c]
        self.assertEqual(polled, [], "a failed `up` must not go on to wait")

    # ---- a stack made from another checkout --------------------------------------

    def test_up_down_and_test_refuse_a_stack_made_from_another_checkout(self):
        other = self.tmpdir.parent / "other-checkout" / "docker"
        other.mkdir(parents=True)
        self.workdir(other)
        self.credentials()
        for command in ("up", "down", "test"):
            self.forget_runs()
            result = self.run_qa(command, OC_CONTRACTS_DIR=str(self.contracts))
            self.assertEqual(result.returncode, 1, command)
            self.assertIn(str(other), result.stderr, command)
            self.assertIn(str(self.code / "docker"), result.stderr, command)
            self.assertIn("QA_ALLOW_FOREIGN_STACK", result.stderr, command)
            self.assertFalse(any(" up -d" in c or c.endswith(" down") or " exec " in c for c in self.calls()), command)
            self.assertFalse((self.stub / "uv.args").exists(), command)

    def test_status_and_wait_work_on_a_stack_made_from_another_checkout(self):
        self.workdir(self.tmpdir.parent / "elsewhere")
        for command in ("status", "wait"):
            result = self.run_qa(command)
            self.assertEqual(result.returncode, 0, (command, result.stderr))

    def test_the_foreign_stack_guard_can_be_overridden_and_has_exact_edges(self):
        other = self.tmpdir.parent / "other-checkout"
        other.mkdir()
        self.workdir(other)
        self.assertEqual(self.run_qa("down", QA_ALLOW_FOREIGN_STACK="1").returncode, 0)
        self.assertEqual(self.run_qa("down", QA_ALLOW_FOREIGN_STACK="yes").returncode, 1)
        for label in ("", str(self.tmpdir.parent / "gone")):
            self.workdir(label)
            self.assertEqual(self.run_qa("down").returncode, 1, label)

    def test_the_guard_accepts_the_same_directory_by_another_name(self):
        link = self.tmpdir.parent / "link-to-docker"
        link.symlink_to(self.code / "docker")
        self.workdir(link)
        self.assertEqual(self.run_qa("down").returncode, 0)

    def test_up_with_no_existing_stack_is_not_a_foreign_stack(self):
        (self.stub / "no-bench").touch()
        self.workdir(self.tmpdir.parent / "never-asked")
        result = self.run_qa("up")
        self.assertIn(f"{self.compose_prefix()} up -d", self.calls())
        self.assertNotIn("QA_ALLOW_FOREIGN_STACK", result.stderr)

    # ---- test: guards ------------------------------------------------------------

    def test_test_requires_a_healthy_bench_and_reads_no_credentials(self):
        self.credentials()
        for state in (STARTING, EXITED):
            self.states([state])
            result = self.run_qa("test", OC_CONTRACTS_DIR=str(self.contracts))
            self.assertEqual(result.returncode, 1)
            self.assertIn("not healthy", result.stderr)
        self.assertFalse(any(" exec " in call for call in self.calls()))
        self.assertFalse((self.stub / "uv.args").exists())

    def test_test_names_the_missing_contracts_before_reading_credentials(self):
        self.credentials()
        result = self.run_qa("test")
        self.assertEqual(result.returncode, 1)
        self.assertIn("OC_CONTRACTS_DIR", result.stderr)
        self.assertFalse(any(" exec " in call for call in self.calls()))

    # ---- test: environment handed to the runner --------------------------------------

    def test_test_exports_what_the_runner_reads_runs_it_and_propagates_its_status(self):
        self.credentials(extra="OTHER=1\nQA_lower=1\nexport QA_EVIL=1\n")
        result = self.run_qa("test", OC_CONTRACTS_DIR=str(self.contracts), STUB_UV_RC="2")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual((self.stub / "uv.args").read_text(), "run scripts/run_p4_live.py\n")
        self.assertEqual((self.stub / "uv.cwd").read_text().strip(), str(self.code))
        seen = self.uv_env()
        self.assertEqual(seen["QA_API_KEY"], CREDENTIALS["QA_API_KEY"])
        self.assertEqual(seen["QA_API_SECRET"], CREDENTIALS["QA_API_SECRET"])
        self.assertEqual(seen["QA_SITE"], "qa.localhost")
        self.assertEqual(seen["OC_BASE"], "http://localhost:8001")
        self.assertEqual(seen["CONTRACTS_DIR"], str(self.contracts))
        for name in ("QA_lower", "QA_EVIL"):
            self.assertEqual(seen[name], "<unset>")
        self.assertTrue(any(" exec -T bench cat /home/frappe/qa-sites/qa-credentials.env" in c for c in self.calls()))

    def test_test_does_not_hand_the_runner_the_admin_password_or_compose_inputs(self):
        self.credentials()
        result = self.run_qa("test", OC_CONTRACTS_DIR=str(self.contracts),
                             QA_ADMIN_PASSWORD="SYN-from-caller", QA_DB_ROOT_PASSWORD="SYN-root")
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = self.uv_env()
        for name in ("QA_ADMIN_USER", "QA_ADMIN_PASSWORD", "QA_DB_ROOT_PASSWORD"):
            self.assertEqual(seen[name], "<unset>", name)

    def test_test_ignores_runner_overrides_and_always_targets_the_qa_stack(self):
        self.credentials()
        result = self.run_qa(
            "test", OC_CONTRACTS_DIR=str(self.contracts), OC_BASE="https://staging.example.invalid",
            OC_API_KEY="SYN-OTHER-KEY", OC_API_SECRET="SYN-OTHER-SECRET", OC_SITE="other.example.invalid")
        self.assertEqual(result.returncode, 0, result.stderr)
        seen = self.uv_env()
        self.assertEqual(seen["OC_BASE"], "http://localhost:8001")
        for name in ("OC_API_KEY", "OC_API_SECRET", "OC_SITE"):
            self.assertEqual(seen[name], "<unset>", name)
        self.assertEqual(seen["QA_API_KEY"], CREDENTIALS["QA_API_KEY"])
        self.assertEqual(seen["QA_SITE"], "qa.localhost")
        for name in ("OC_API_KEY", "OC_API_SECRET", "OC_SITE", "OC_BASE"):
            self.assertIn(name, result.stderr)
        for value in ("SYN-OTHER", "staging.example.invalid", "other.example.invalid"):
            self.assertNotIn(value, result.stderr)

    def test_test_does_not_let_the_callers_qa_values_stand_in_for_the_stacks(self):
        self.credentials({"QA_API_KEY": "SYN_key_1", "QA_API_SECRET": "SYN_secret_1"})
        result = self.run_qa("test", OC_CONTRACTS_DIR=str(self.contracts), QA_SITE="other.example.invalid")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.uv_env()["QA_SITE"], "<unset>")

    def test_test_never_prints_or_stores_credentials(self):
        self.credentials()
        for shell in ((), ("-x",)):
            result = self.run_qa("test", shell=shell, OC_CONTRACTS_DIR=str(self.contracts))
            self.assertEqual(result.returncode, 0, result.stderr)
            for value in (*RUNNER_VALUES, CREDENTIALS["QA_ADMIN_PASSWORD"]):
                self.assertNotIn(value, result.stdout + result.stderr, shell)
        self.assertEqual(list(self.tmpdir.iterdir()), [])

    # ---- test: contracts directory ------------------------------------------------------

    def test_test_leaves_an_explicit_contracts_dir_alone_and_resolves_relative_ones(self):
        self.credentials()
        elsewhere = self.contracts.parent / "elsewhere"
        result = self.run_qa("test", CONTRACTS_DIR=str(self.contracts), OC_CONTRACTS_DIR=str(elsewhere))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.uv_env()["CONTRACTS_DIR"], str(self.contracts))
        # A relative OC_CONTRACTS_DIR means what it means to compose: relative to docker/.
        self.forget_runs()
        relative = os.path.relpath(self.contracts, self.code / "docker")
        result = self.run_qa("test", OC_CONTRACTS_DIR=relative)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.uv_env()["CONTRACTS_DIR"], str(self.contracts))

    def test_test_makes_a_relative_contracts_dir_absolute_from_where_it_was_typed(self):
        self.credentials()
        relative = os.path.relpath(self.contracts, self.tmpdir)
        result = self.run_qa("test", CONTRACTS_DIR=relative)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.uv_env()["CONTRACTS_DIR"], str(self.contracts))

    def test_test_exports_the_checked_path_even_when_contracts_dir_was_empty(self):
        self.credentials()
        beside = self.code.parent / "spec" / "contracts"
        beside.mkdir(parents=True)
        (beside / "intake-census.yaml").write_text("SYN: census\n")
        result = self.run_qa("test", CONTRACTS_DIR="")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.uv_env()["CONTRACTS_DIR"], str(beside))

    def test_test_follows_the_code_legs_own_contracts_first_like_the_runner(self):
        self.credentials()
        own = self.code / "contracts"
        own.mkdir()
        (own / "intake-census.yaml").write_text("SYN: census\n")
        result = self.run_qa("test", CONTRACTS_DIR=str(self.contracts))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.uv_env()["CONTRACTS_DIR"], str(own))

    # ---- test: reading the credentials file -----------------------------------------------

    def test_test_fails_when_the_credentials_file_is_missing_or_incomplete(self):
        env = {"OC_CONTRACTS_DIR": str(self.contracts)}
        result = self.run_qa("test", **env)
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot read", result.stderr)
        for values in ({"QA_SITE": "qa.localhost"}, {"QA_API_KEY": "SYN_k"}, {"QA_API_SECRET": "SYN_s"},
                       {"QA_API_KEY": "", "QA_API_SECRET": "SYN_s"}, {"QA_API_KEY": "SYN_k", "QA_API_SECRET": ""}):
            self.credentials(values)
            result = self.run_qa("test", **env)
            self.assertEqual(result.returncode, 1, values)
            self.assertIn("lacks a non-empty QA_API_KEY or QA_API_SECRET", result.stderr, values)
            self.assertFalse((self.stub / "uv.args").exists(), values)

    def test_test_refuses_a_repeated_key(self):
        for name in ("QA_API_KEY", "QA_API_SECRET", "QA_SITE"):
            for value in ("SYN_again", ""):
                self.credentials(extra=f"{name}={shlex.quote(value)}\n")
                result = self.run_qa("test", OC_CONTRACTS_DIR=str(self.contracts))
                self.assertEqual(result.returncode, 1, (name, value))
                self.assertIn(f"{name} appears more than once", result.stderr)
                self.assertFalse((self.stub / "uv.args").exists(), name)

    def test_test_refuses_a_value_that_is_not_one_shlex_word_without_running_it(self):
        sentinel = self.tmpdir.parent / "SENTINEL"
        values = (f"$(touch {sentinel})", f"a;touch {sentinel}", f"`touch {sentinel}`", "x y",
                  "'unterminated", "\"x\"", "'a'b c", "a'")
        for name in ("QA_API_KEY", "QA_API_SECRET", "QA_SITE"):
            for value in values:
                good = {k: v for k, v in CREDENTIALS.items() if k != name}
                self.credentials(good, extra=f"{name}={value}\n")
                result = self.run_qa("test", OC_CONTRACTS_DIR=str(self.contracts))
                self.assertEqual(result.returncode, 1, (name, value))
                self.assertIn(f"credentials entry {name} ", result.stderr)
                self.assertNotIn(value, result.stdout + result.stderr)
                self.assertFalse(sentinel.exists(), value)
                self.assertFalse((self.stub / "uv.args").exists(), (name, value))

    def test_test_ignores_other_qa_lines_even_when_they_are_not_shell_safe(self):
        sentinel = self.tmpdir.parent / "SENTINEL"
        self.credentials(extra=f"QA_EVIL=$(touch {sentinel})\nQA_ADMIN_USER='unterminated\n")
        result = self.run_qa("test", OC_CONTRACTS_DIR=str(self.contracts))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(sentinel.exists())
        self.assertEqual(self.uv_env()["QA_EVIL"], "<unset>")


if __name__ == "__main__":
    unittest.main()
