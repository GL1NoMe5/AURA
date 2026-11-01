# Review dispositions (OPS-FINAL-ACCEPT-001)

Finite closure of the five formerly uncertain Argon entries plus the
retained 5.3 trust boundary. Each item was re-checked against the original
`.local/argon/review.md` entry and the current code/docs on 2026-10-30.
This is a disposition ledger, not a new security scan.

## Disposition table

| Item | Argon entry | Disposition | Evidence |
| --- | --- | --- | --- |
| 2.4 | `fix_proposed` outside `ACTIVE` lets a later investigation run over a ready patch | Accepted product behavior (MVP) | `backend/aura_ops/service.py` |
| 2.5 | Nested promotion loses `project_directory`/`.env` | Pass: no defect with installed Compose v5.6.1 | Config experiment, this task |
| 4.2 | No in-transfer sample when a download finishes fast | Accepted accounting: `in_transfer_samples` / `no_in_transfer_sample`, sampled peak only | `backend/aura_ops/verification.py`, `docs/VERIFICATION_EVIDENCE.md` |
| 4.3 | Startup failures absorbed into `baseline_*` | Accepted MVP limit: coverage starts at the observed workload interval | Same files |
| 4.4 | Failed deploy recorded as `rolled_back` without checking post-rollback health | Accepted meaning: `rolled_back` = old image restored, not healthy; `verified` stays false, phase stays `needs_human` | `backend/aura_ops/service.py` |
| 5.3 | JSON/nonce/readonly/AST vs hostile in-interpreter Python | Retained boundary: not authentication, not adequacy proof | `docs/TEST_EVIDENCE_PROTOCOL.md` |

## 2.4 — a ready patch does not freeze monitoring (accepted behavior)

`ACTIVE` (`backend/aura_ops/service.py:25`) still excludes `fix_proposed`,
so monitoring continues and a later investigation for the same target may
be created while a tested patch waits. This is the intended product
behavior:

- The monitor loop keeps observing every connected target regardless of
  patch state (`_monitor`, `service.py:384-419`); automatic investigation
  additionally requires explicit `auto_investigate` enrollment plus a ready
  provider, and is refused while any unknown outcome is unresolved
  (`service.py:405-413`, `start_investigation`, `service.py:616-634`).
- Serialization is explicit, not destructive: `prepare_deployment` refuses
  with `operation_busy` while any `ACTIVE` work or a busy task exists
  (`service.py:1045-1047`), and `_kick` runs one investigation task at a
  time (`service.py:636-650`). The tested patch record stays on its own
  investigation row; `prepare_patch` only starts from `needs_human` /
  `cancelled` (`PREPARE_PATCH_ALLOWED_PHASES`, `service.py:30`), so a later
  investigation cannot overwrite the ready fix.
- A ready patch cannot silently deploy. Automatic deployment is rejected
  (`deployment_approval_required`, `service.py:777-779`); `decide` requires
  an exact proposal hash, `proposed` status, `awaiting_approval` phase and
  an idle task (`service.py:733-746`). Build, execute and final dispatch
  each revalidate identity, snapshot, patch binding and authority, failing
  closed with `target_changed` / `config_changed` instead of deploying
  onto a changed target (`backend/aura_ops/deployment.py:388-389`,
  `428-432`, `493-501`, `537-538`, `571-580`).
- Automatic restarts (the Argon overwrite scenario) require explicit
  `policy.automatic_actions` enrollment — enrollment itself is observe-only
  (`service.py:337-338`) — and share the restart cooldown checked at
  proposal, preflight and final dispatch. Even if one fires, it changes
  container identity, which the deployment revalidation above rejects
  rather than silently building on.

No code change; the behavior and its limits are recorded here.

## 2.5 — nested promotion keeps paths, env, networks, volumes (pass)

Argon claimed a second promotion breaks relative bind mounts (wrong
`project_directory`) and drops `.env` variables (forced `empty.env`).
The config-only experiment in this task disproves it for the installed
stack (Docker 29.9.2, Compose v5.6.1, engine Linux):

- Two nested promotions ran through production `promotion.promote` with
  real store, snapshot capture, `_check_snapshot`, `_approved` and
  `promote_baseline`; only target identity and patch verification were
  mocked. The second include really nests (`project_directory` = first
  version dir, `env_file` = empty env), matching Argon's setup.
- Real `docker compose config --format json` on base, first and second
  configs: relative binds resolve to the original absolute paths,
  `env_file` entries and `${...}` interpolation values are equal,
  explicit network/volume names and attachments are preserved, and the
  first/second resolved documents match byte-for-byte. The only base
  difference is the expected image plus the enrolled direct launch.
- Each include level keeps its own `project_directory`/`env_file`, so the
  nested resolution stays correct. Additionally, `_check_snapshot`
  (`backend/aura_ops/actions.py:263-281`) re-hashes the enrolled files
  and the fully resolved config (`resolved_sha256`) before every replay,
  so an external edit to the original files is still detected.

Full commands, versions, values and comparisons:
`.local/final-acceptance/OPS-FINAL-ACCEPT-001/config-experiment.json`
plus `compose-base/first/second.json` and `compose-runner-calls.json`.
Bound: this verdict covers the installed Compose semantics for this
include shape; it is not a claim about every future Compose version.

## 4.2 — in-transfer accounting is explicit (accepted behavior)

`verify_functional` (`backend/aura_ops/verification.py:182-184`,
`365-366`) records `in_transfer_samples` and `transfer_coverage`
(`sampled` vs `no_in_transfer_sample`); a transfer that finishes between
polls is reported as uncovered, not invented. `sampled_peak_memory_bytes`
is documented as the maximum of recorded valid samples, never an absolute
peak, and a verified result is not a continuous memory profile
(`docs/VERIFICATION_EVIDENCE.md:50-65`). Sampling blind spots remain an
explicit residual. No overclaim found in current code or docs.

## 4.3 — coverage starts at the observed interval (accepted MVP limit)

Baselines (`baseline_failures`, `baseline_restart_count`,
`baseline_oom_killed`, `verification.py:299-304`) are still taken when
functional monitoring starts, and only lines/events added during the
monitored interval fail the run. Historical lines stay visible separately
(`baseline_worker_failures`), and failures from container startup before
monitoring began are explicitly out of scope
(`docs/VERIFICATION_EVIDENCE.md:67-73`). This is an MVP limit, not a
claim that every earlier worker failure was ruled out.

## 4.4 — `rolled_back` means restored, not healthy (accepted meaning)

On failed verification with `rollback_on_failure`, `_deploy` rolls back,
re-verifies availability, records `status="rolled_back"` with the full
verification object, and moves the investigation to `needs_human`
(`backend/aura_ops/service.py:1106-1147`). The record keeps
`verified: false` (computed before rollback) and the post-rollback
availability result under `verification.rollback.availability`, so a
failed restore cannot read as success: `rolled_back` asserts the old
image was restored, never that the application is healthy. The UI treats
it as attention (`Change: Rolled back`), not success
(`frontend/src/ops/sceneSteps.ts:34`). Meaning preserved; no code change.

## 5.3 — retained trust boundary (not closed, documented)

Unchanged from `docs/TEST_EVIDENCE_PROTOCOL.md:102-113`: the structured
report envelope, nonces, read-only mounting and AST denylists block
accidental collisions and direct named bypasses only. Arbitrary hostile
Python sharing pytest's interpreter can still read argv/env, dodge static
checks via indirection, or suppress the envelope; JSON reports do not
authenticate such code and do not establish semantic test adequacy. Human
patch review and independent functional verification remain required.
No new claim is made here.
