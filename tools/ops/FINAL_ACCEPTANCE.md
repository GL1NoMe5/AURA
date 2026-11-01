# OPS-FINAL-ACCEPT-001 evidence harness

`tools/ops/final_acceptance.py` produces the finite post-guard evidence bundle.
It has three subcommands and performs no other work.

## Usage

Run from the AURA project root in PowerShell with the project Python:

```powershell
$py = "backend/.venv/Scripts/python.exe"
$ev = ".local/final-acceptance/OPS-FINAL-ACCEPT-001"

& $py tools/ops/final_acceptance.py versions --evidence-dir $ev
& $py tools/ops/final_acceptance.py config-experiment --evidence-dir $ev
& $py tools/ops/final_acceptance.py run-matrix --evidence-dir $ev --run-id acc-<id>
```

`config-experiment` refuses to run twice into the same evidence directory.
`run-matrix` allows one matrix run plus at most one retry, and the retry
requires `--retry-harness-defect` with a description of the demonstrated
harness-only defect (at least 24 characters). A passed matrix is never
repeated.

## Config experiment (Argon 2.5)

Two nested baselines are produced through the real production promotion code
(`backend/aura_ops/promotion.py::promote`) with real `OpsStore`,
`ActionExecutor` (compose hashing/resolution), `DeploymentManager._snapshot`
/ `_approved` and `store.promote_baseline`. Only two boundaries are mocked:

- target identity (`DeploymentManager._container`, `collect`,
  `inspect_identity`) returns one fixed invented container/image identity;
- verification (`DeploymentManager._revalidate`) returns a fixed checked
  patch record, base image and candidate image.

The Docker boundary is fail-closed: `compose config` and `image inspect`
run against the real installed binaries, every other verb raises, and every
call is recorded in `compose-runner-calls.json`. `compose config` never
starts containers. The base tree uses harmless invented values (see
`FIXTURE` in the harness): a relative bind mount, `env_file` plus
interpolation, and explicitly named networks/volumes. Base, first and second
promoted configs live in separate directories and the comparison runs from a
neutral working directory.

The comparison requires: relative binds resolve to the original absolute
paths, `env_file`/interpolation values stay equal, named networks/volumes
keep their names, and only the expected image plus the enrolled direct
launch differ. Any other drift reports `confirmed-problem` with exact
values; the harness never edits production code.

## Matrix run

`run-matrix` reuses `http://127.0.0.1:8000/health` only when it already
returns HTTP 200. Otherwise it launches a temporary production `create_app`
instance on a verified free localhost port with `provider_configured=False`,
`wire_default_orchestrator=False`, `enable_demo=False`, `enable_ops=False`
(API liveness only; the matrix uses its own isolated `OpsService`/store),
points only this invocation at it via `AURA_ACCEPT_BACKEND_HEALTH`, and
stops exactly the child process it started afterwards. It never stops,
restarts or kills any other listener; an occupied port is not permission
to take it.

The matrix itself uses `tools/ops/acceptance.py` scenarios
(`model_mode=deterministic_mock`, zero provider requests) with labelled
disposable targets, read-only source copying, ownership-checked cleanup and
before/after registered-target fingerprints. Codex added explicit `--cases`
selection and checks the current early cooldown block contract. A selected
run records its scope and never claims all six cases. Independent config
faults use new disposable fixtures without clearing cooldown state.

The wrapper requires exit 0, the exact requested case IDs, all scenario gates,
nonempty confirmed cleanup, unchanged registered fingerprints and final backend
HTTP 200. A temporary backend must also have its owned process tree gone and
port free after bounded rechecking. Exceptions/timeouts save failed attempt
evidence; killing a process is not claimed to clean up Docker resources.

## Guarantees and limits

- No provider requests, installs, image builds/pulls, UI changes,
  production code edits, 20-minute soak, publication or submission.
- Registered fixtures, gateways, volumes and `.local/ops` are never
  mutated; selected evidence documents are checked for credential patterns.
  That is not a blanket claim that every binary/log has been secret-scanned.
- A product-gate rejection is not a harness defect and is never bypassed
  or retried as one.
- The temporary backend proves API liveness only. Mocked diagnostic
  decisions prove orchestration plumbing, not model quality.

## Focused tests

```powershell
backend/.venv/Scripts/python.exe -m pytest backend/tests/test_ops_final_acceptance.py -q
```

Offline only: resolved-config extraction/comparison, secret scanning,
attempt-budget slots, the fail-closed runner boundary (stubbed command
layer), the fixture writer, the mocked-identity shape against the
production validator, and the health-URL injection. Run these before any
real invocation.
