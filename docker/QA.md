# Persistent openChart QA site

This stack runs a synthetic, single-site Frappe QA deployment with only
`open_chart` installed. It uses the same pinned Frappe/Python/MariaDB/Redis
matrix as the throwaway Docker gate.

## Bring up

Run every command in this document from the root of the code leg: the
directory that holds `docker/`, `open_chart/` and `pyproject.toml`. In an
openChart assembly checkout that is `code/`; in a standalone `openChart-code`
clone it is the top level.

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
  `open_chart`.

The stack also needs the spec leg's `contracts/` directory, which holds the
intake census that `open_chart.api.v1` loads on every call. The compose file
mounts it read-only at `/workspace/contracts` and gives the bench
`CONTRACTS_DIR=/workspace/contracts`. Inside an assembly checkout the default
(`../../spec/contracts`, relative to `docker/`) is the spec leg's `contracts/`,
so there is nothing to set. A standalone `openChart-code` clone has no spec leg
beside it: clone `opensoft/openChart-spec` and point `OC_CONTRACTS_DIR` at its
`contracts/` directory by absolute path. Only `up` needs it.

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
- The bootstrap creates the System User `qa-runner@qa.localhost`, with username
  `qa-runner`, and grants the `System Manager` role.
- API key, API secret, and the creation-time admin password are written with
  mode `0600` to `/home/frappe/qa-sites/qa-credentials.env` inside the bench
  container. That path is on the `qa-sites` named volume, so it survives
  container replacement and ordinary `docker compose down`/restart cycles.

Read the generated credentials without copying them into any openChart
repository (the assembly root, the spec leg, or the code leg):

```bash
docker compose -f docker/qa-compose.yaml exec bench \
  cat /home/frappe/qa-sites/qa-credentials.env
```

## Persistence and clean-slate reruns

An ordinary stop preserves the named database, bench-home, and sites volumes:

```bash
docker compose -f docker/qa-compose.yaml down
```

To destroy the site, database, installed apps, and generated credentials:

```bash
docker/qa-teardown.sh
```

The teardown uses `down --volumes`; this is intentionally destructive. The next
`up -d` performs a clean-slate initialization. Repeated starts without teardown
skip existing bench/site/app/user work and still run migrations.

## Boundary

This is a persistent **synthetic QA** deployment for real-system test runs. It
is not production, is not hardened for remote exposure, and must contain no
real PHI, patient uploads, production credentials, or decrypted clinical data.

## Live runner credentials

The live runner is `scripts/run_p4_live.py` in the code leg. From the code leg
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
reads; `OC_CONTRACTS_DIR` above is read only by the compose files.

The runner accepts the generated `QA_API_KEY`, `QA_API_SECRET` and `QA_SITE`
variables as defaults. Explicit `OC_API_KEY`, `OC_API_SECRET` and `OC_SITE`
values take precedence. Load the protected credential file into the runner's
environment without shell tracing or printing it. The isolated `uv run` path
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
