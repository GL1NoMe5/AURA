# Real-Docker acceptance with an offline diagnostic provider

`tools/ops/acceptance.py` provisions two disposable sandbox applications
(Linkding config sandbox, Healthchecks restart sandbox) and drives six
lifecycle scenarios through production orchestration: real Docker/HTTP and
actual `OpsService` / `ActionExecutor` / `DockerTarget` / `OpsStore`.
Only model completion bytes are canned (`model_mode=deterministic_mock`)
through the real `OpsInvestigator` validation. No Baseten/Meta requests.

## Usage

Run from the AURA project root in PowerShell:

```powershell
backend/.venv/Scripts/python.exe tools/ops/acceptance.py
```

Default is read-only preflight: engine/images/ports/provenance checks plus
registered-fixture fingerprints and backend `/health`. No fixtures are created.

```powershell
backend/.venv/Scripts/python.exe tools/ops/acceptance.py --run-real-docker
```

Runs preflight, provisions labelled sandbox resources, executes the matrix
once, verifies registered fixtures and backend health afterwards, removes
only owned resources, and writes evidence to
`.local/ops-acceptance/<run-id>/result.json`. Exit 0 means passed; anything
else means failed or blocked with evidence preserved.

The shared 600-second restart/restore cooldown now applies at proposal creation.
Configuration-fault cases 3 and 4 need independent freshly provisioned fixtures
to both demonstrate recovery. Running both back-to-back on the same target
correctly blocks the latter; do not clear its cooldown or rewrite timestamps.
Use explicit subsets, for example `--cases 1,2,3,5,6` and, on a separate new
run ID, `--cases 1,4,5,6`. Every subset includes enrollment case 1 and records
`scope=selected` plus `selected_cases`; its pass never means all six ran.
Previously completed evidence can be combined only with this distinction stated.

Case 5 requires the second proposal to be `blocked` with `restart_cooldown`,
manual approval to reject, no dispatch marker, an unchanged durable cooldown,
and unchanged container/image/StartedAt after the first verified restart.
An unrelated error or late failed dispatch does not satisfy this check.

```powershell
backend/.venv/Scripts/python.exe -m pytest backend/tests/test_ops_live_acceptance.py -q
```

Focused offline tests: ownership/cleanup fail-closed behavior, port refusal,
deterministic-provider contract, compose isolation, evidence recording. They
use faked Docker boundaries and never touch real Docker.

## Guarantees and limits

- Disposable fixtures only: unique `org.aura.accept=<run-id>` labels on every
  created container/network/volume; localhost-only ports picked from free
  18093-18096 (occupied ports refused); at most two sandbox apps.
- Registered Linkding/Healthchecks fixtures, their gateways and data volumes,
  the backend on port 8000, and `.local/ops` are never mutated; container,
  image and StartedAt fingerprints are asserted before and after.
- Original data is copied through read-only mounts. Exact database/nonempty-WAL
  fingerprints must match before SQLite opens the clone; an empty WAL is ignored.
  `integrity_check` then opens only the clone normally, including committed WAL
  frames. A checkpoint may legitimately change clone bytes afterwards. Source
  fingerprints must stay stable throughout. At most four bounded copy attempts.
  The copy helper runs as root with only CHOWN/DAC_OVERRIDE, no network and a
  read-only source mount, to read and preserve fixture file ownership.
- Scripted operator approvals inside the matrix are authorized for these
  disposable fixtures only and recorded with exact proposal hashes.
- A mocked abstention proves the orchestration path, not model accuracy; a
  mocked restore/restart decision proves proposal/approval/execution/
  verification plumbing with real measurements, not diagnostic quality.
  Final live-model acceptance belongs to Codex.
- The matrix budget is 8 minutes excluding bounded provisioning. Product-gate
  blocks are recorded as blocked/failed; gates are never bypassed and such a
  run is never reported passed.
- Cleanup runs in `finally` and verifies owner labels before every removal;
  any leftover labelled resource fails the run. Evidence is preserved.
  Exact reserved Compose names are reconciled to current owned IDs because
  restoration recreates containers. Foreign replacements are never removed.

## Verified run (2026-10-28)

Codex reviewed and repaired provisioning/runner defects after Wonder's blocked
handoff. `.local/ops-acceptance/acc-65f068e16495/result.json` passed all six cases
in 104.9 seconds of scenarios (123.2 seconds total). All disposable resources
were removed, registered fixtures were unchanged, backend remained healthy,
and provider requests were zero. The deterministic diagnostic decisions do not
measure model quality. Focused runner regression tests: 25 passed.
