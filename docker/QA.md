# Persistent openChart QA site

This stack runs a synthetic, single-site Frappe QA deployment with only
`open_chart` installed. It uses the same pinned Frappe/Python/MariaDB/Redis
matrix as the throwaway Docker gate.

## Commands

`docker/qa.sh` runs the usual operations in one command. It resolves every
path from its own location, so it works from any directory: as
`code/docker/qa.sh` in an openChart assembly checkout and as `docker/qa.sh` in
a standalone `openChart-code` clone.

```bash
docker/qa.sh up       # start the stack, then wait until the bench is healthy
docker/qa.sh test     # run the live runner against the healthy bench
docker/qa.sh status   # show the stack's containers
docker/qa.sh down     # stop the stack; the data stays
```

- `up` runs `docker compose -f docker/qa-compose.yaml up -d`, then `wait`.
  `QA_ADMIN_PASSWORD`, `QA_DB_ROOT_PASSWORD` and `OC_CONTRACTS_DIR` reach
  compose as described below.
- `wait` polls every `QA_POLL_INTERVAL` seconds (default 5) until the bench
  container is healthy, for at most `QA_UP_TIMEOUT` seconds (default 1200). A
  healthy bench returns at once. It fails at once, with exit status 1, when
  the bench exits, is paused or restarting, restarts, turns unhealthy, stays
  `created` for `QA_CREATED_GRACE` seconds (default 30) or logs
  `OC-QA-RESULT: FAIL`, and then prints the bench's last `== OC-QA-STEP` lines
  and any `OC-QA-RESULT` line from the runs it watched.
- `test` needs a healthy bench and `uv`. It reads the generated credentials
  out of the bench container, exports `QA_API_KEY`, `QA_API_SECRET` and
  `QA_SITE` (the rest of the file, the Administrator password included, stays
  behind), and runs `uv run scripts/run_p4_live.py` from the code leg root; its
  exit status is the runner's. The credentials stay in memory: `test` never
  prints, traces or writes them to disk. It ignores `OC_API_KEY`,
  `OC_API_SECRET`, `OC_SITE` and `OC_BASE` in your environment, and says so,
  and always targets `http://localhost:8001`, so records go only to the stack
  it just checked. It also puts `localhost` and `127.0.0.1` first in
  `no_proxy` and `NO_PROXY` (keeping what is there), so a proxy variable in
  your environment cannot carry the runner's credentials off the machine. When
  `CONTRACTS_DIR` is unset and `OC_CONTRACTS_DIR` is set, it uses the latter as
  `CONTRACTS_DIR`. "Live runner credentials" below says where the evidence
  goes.
- `down` runs `docker compose -f docker/qa-compose.yaml down`. It never
  removes volumes; destroying the data is `docker/qa-teardown.sh`.
- `status` runs `docker compose -f docker/qa-compose.yaml ps`.
- `help`, or no command, prints the usage text. An unknown command prints it
  to stderr and exits with status 2.

Every compose call in `qa.sh` also passes `-p open-chart-qa`, the project the
compose file names, so `COMPOSE_PROJECT_NAME`, a `docker/.env` file or
`COMPOSE_ENV_FILES` cannot point it at another stack. `up`, `down`, `test` and
`docker/qa-teardown.sh` refuse a stack that was created from another checkout,
which they recognize by the compose working directory recorded on its bench
container (on any other container of the project when there is no bench), and
the message names both directories. `status` and `wait` still work on it. Set
`QA_ALLOW_FOREIGN_STACK=1` to operate on it from here anyway.

A standalone clone needs the spec leg's contracts for both `up` and `test`
(see "Bring up"):

```bash
export OC_CONTRACTS_DIR=/abs/path/to/openChart-spec/contracts
docker/qa.sh up && docker/qa.sh test
```

## Bring up

`docker/qa.sh up` does this from any directory. The raw `docker compose`
commands in this document run from the root of the code leg: the directory
that holds `docker/`, `open_chart/` and `pyproject.toml`. In an openChart
assembly checkout that is `code/`; in a standalone `openChart-code` clone it
is the top level.

```bash
cd code   # assembly checkout only; a standalone clone is already there
QA_ADMIN_PASSWORD=oc-qa-admin docker compose -f docker/qa-compose.yaml up -d
docker compose -f docker/qa-compose.yaml ps
```

The compose file bind-mounts the directory above `docker/` read-only at
`/workspace/open_chart`, and the bench copies the app from it, so that
directory has to be the code leg. Compose resolves it from the compose file's
path as written:

- `-f docker/qa-compose.yaml` from the code leg root mounts the code leg.
- `-f code/docker/qa-compose.yaml` from the assembly root mounts the same
  directory, so it is equivalent. Use `code/docker/qa-teardown.sh` there.
- Do not run `-f docker/qa-compose.yaml` from the assembly root through a
  `docker -> code/docker` symlink (an untracked convenience some checkouts
  carry). Compose does not follow the link, so it mounts the whole assembly
  root as the app, and the bench would copy and install that instead of
  `open_chart`. `qa.sh` resolves symlinks first, so it is safe there.

The stack also needs the spec leg's `contracts/` directory, which holds the
intake census that `open_chart.api.v1` loads on every call. The compose file
mounts it read-only at `/workspace/contracts` and gives the bench
`CONTRACTS_DIR=/workspace/contracts`. Inside an assembly checkout the default
(`../../spec/contracts`, relative to `docker/`) is the spec leg's `contracts/`,
so there is nothing to set. A standalone `openChart-code` clone has no spec leg
beside it: clone `opensoft/openChart-spec` and point `OC_CONTRACTS_DIR` at its
`contracts/` directory by absolute path. `up` needs it, and `qa.sh test`
reads it too, as the runner's `CONTRACTS_DIR`.

```bash
OC_CONTRACTS_DIR=/abs/path/to/openChart-spec/contracts \
  QA_ADMIN_PASSWORD=oc-qa-admin docker compose -f docker/qa-compose.yaml up -d
```

A one-shot `contracts-check` service runs before the bench. When the mounted
directory has no `intake-census.yaml`, `up` fails with `service
"contracts-check" didn't complete successfully`, and `docker compose -f
docker/qa-compose.yaml logs contracts-check` shows `OC-QA-RESULT: FAIL at step
'contracts mount'`. That beats a bench that starts and fails on its first API
call. Docker Desktop creates a missing mount source as an empty directory rather
than refusing, so a mistyped path is caught by this check too; remove the stray
directory it leaves behind.

The first start initializes the bench and can take several minutes. Compose
does not report the bench healthy until `http://qa.localhost:8001` answers the
Frappe ping endpoint. Open the site at that URL. The fixed host port is `8001`.

Bring-up order is automatic: MariaDB becomes healthy, both Redis services start,
the contracts check passes, then the bench initializes `qa.localhost`, installs
`open_chart`, runs migrations, bootstraps credentials, and finally starts
Gunicorn.

The bench container runs Gunicorn on `0.0.0.0:8000` rather than `bench start`.
This is the simplest reliable long-lived HTTP process for the pinned Bench
image and avoids starting duplicate local Redis processes when this stack is
already wired to its Compose Redis services.

## Configuration and credentials

- `QA_ADMIN_PASSWORD` sets the Administrator password when the site is first
  created (default: `oc-qa-admin`). Changing it later does not rotate an
  existing site's password; wipe and recreate the stack to apply a new value.
- `QA_DB_ROOT_PASSWORD` sets the QA MariaDB root password (default:
  `oc-qa-root`). Use the same value whenever restarting an existing stack.
- Compose reads both variables on every `up`, so a different value recreates
  the containers that use it, though only a new site takes the admin password.
- The bootstrap creates the System User `qa-runner@qa.localhost`, with username
  `qa-runner`, and grants the `System Manager` role.
- API key, API secret, and the creation-time admin password are written with
  mode `0600` to `/home/frappe/qa-sites/qa-credentials.env` inside the bench
  container. That path is on the `qa-sites` named volume, so it survives
  container replacement and ordinary `docker compose down`/restart cycles.

`docker/qa.sh test` reads this file for the live runner. To read the
generated credentials yourself, without copying them into any openChart
repository (the assembly root, the spec leg, or the code leg):

```bash
docker compose -f docker/qa-compose.yaml exec bench \
  cat /home/frappe/qa-sites/qa-credentials.env
```

## Persistence and clean-slate reruns

An ordinary stop (`docker/qa.sh down`) preserves the named database,
bench-home, and sites volumes. It runs:

```bash
docker compose -f docker/qa-compose.yaml down
```

To destroy the site, database, installed apps, and generated credentials:

```bash
docker/qa-teardown.sh
```

The teardown uses `down --volumes`; this is intentionally destructive. It takes
the project name and the foreign-checkout guard from `qa.sh`, which it sources,
so it too refuses a stack created from another checkout unless
`QA_ALLOW_FOREIGN_STACK=1`. The next `up -d` performs a clean-slate
initialization. Repeated starts without teardown skip existing
bench/site/app/user work and still run migrations.

## Boundary

This is a persistent **synthetic QA** deployment for real-system test runs. It
is not production, is not hardened for remote exposure, and must contain no
real PHI, patient uploads, production credentials, or decrypted clinical data.

## Live runner credentials

The live runner is `scripts/run_p4_live.py` in the code leg, and
`docker/qa.sh test` runs it against the QA stack. By hand, from the code leg
root, run `uv run scripts/run_p4_live.py`; from the assembly root, run
`uv run code/scripts/run_p4_live.py`. Add `--self-test` for the offline check.
Only that script path depends on the working directory: the runner finds its
code leg from its own location, so it behaves the same wherever it is started
from.

A run writes its evidence record to
`specs/001-expanded-patient-intake/live-evidence/<run-id>/evidence.yaml` under
the spec leg's root. That is `spec/` in an assembly checkout, so
`spec/specs/001-expanded-patient-intake/live-evidence/<run-id>/evidence.yaml`
from the assembly root, and the code leg root itself in a standalone
`openChart-code` clone, so `specs/001-expanded-patient-intake/live-evidence/`
beside `docker/`. The `PASS:` or `FAIL:` line the runner prints gives that path
relative to the same spec leg root. The raw request log,
`var/p4-live/<run-id>/http.jsonl`, goes under the code leg root in both layouts
(`code/var/p4-live/` in an assembly checkout) and is git-ignored.

The runner reads the intake census from the spec leg's `contracts/` directory,
`../spec/contracts` relative to the code leg root by default. That is the spec
leg in an assembly checkout, so nothing needs setting there. A standalone clone
has no spec leg beside it, and `--self-test` fails on the missing
`intake-census.yaml` until `CONTRACTS_DIR` is set to the absolute path of the
spec leg's `contracts/` directory. `CONTRACTS_DIR` is the variable the runner
reads. `OC_CONTRACTS_DIR` above is read by the compose files and by `qa.sh`,
whose `test` hands it to the runner as `CONTRACTS_DIR` when that is unset.

Run by hand, the runner accepts the generated `QA_API_KEY`, `QA_API_SECRET` and
`QA_SITE` variables as defaults, and explicit `OC_API_KEY`, `OC_API_SECRET` and
`OC_SITE` values take precedence. `docker/qa.sh test` is stricter: it takes
`QA_API_KEY`, `QA_API_SECRET` and `QA_SITE` from the stack's credential file
and ignores `OC_API_KEY`, `OC_API_SECRET`, `OC_SITE` and `OC_BASE`, so a value
left in your environment cannot send records to another server. To run the
runner by hand, load the protected credential file into its environment without
shell tracing or printing it. The isolated `uv run` path
installs its declared PyYAML dependency; offline self-test opens no sockets.

Recovery records omit response headers; exact original historical bytes remain
in the private cleanup checkpoint. Offline `uv run` needs PyYAML already cached
when package-network access is unavailable.

New standalone runner records are diagnostic scenario evidence and set
`feeds_acceptance_report: false`. A Frappe version probe does not attest exact
deployed source commits or the internal adapter route. Verify those separately
from the deployed source/configuration before using a record for acceptance;
a scenario PASS alone does not supply that provenance. Endpoint URLs must not
contain userinfo, query tokens or fragments. Plain HTTP is limited to the
synthetic QA topology described here; production transport is outside this gate.
