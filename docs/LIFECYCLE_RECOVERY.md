# Lifecycle recovery and retry contract (OPS-LIFECYCLE-GUARD-001)

Offline backend guards for patch preparation, baseline capture and restart
recovery. No UI, portrait, audio or tutorial changes. No Docker mutations,
model calls, network, installs or live target changes on startup.

## Codex review corrections — 2026-10-30

Independent review reproduced seven failing cases, grouped into four gaps:

- Collection-time disconnect/source/investigation changes could start patch work
  with the previous enrollment. Entry now rechecks the complete spec, investigation
  and active work after the awaited observation and before phase/job creation.
- A failed or cancelled deployment build retained a tested patch but stranded both
  retry paths. `prepare_deployment` now accepts that retained patch from
  `needs_human`/`cancelled` only after the same binding/no-dispatch/no-unknown checks.
  It expires never-dispatched old proposals and rebuilds for fresh approval. It
  never regenerates the patch or bypasses the existing deployment validator.
  The UI offers Review fix for these retained candidates; completed investigations
  no longer offer Test a fix.
- If startup itself stopped between investigation repair and unknown-outcome
  persistence, the original active phase was lost. Startup now commits proposal
  unknown gates before changing investigation phases. Repeating startup does not
  create another monitor while its existing monitor is alive.
- A successfully verified deployment proposal committed just before its
  investigation phase could lose its promotion path. For a matching target,
  investigation and patch, complete stored availability/functional success now
  restores `verified_fixed` and leaves explicit baseline promotion retry. This
  reuses historical evidence; it performs no new verification or promotion.

Original Wonder evidence is retained. Independent final focus: 160 passed, one
existing Starlette/httpx warning; 37 lifecycle-specific cases within that run.
The 18 new Codex cases include negative retry and verified-record binding checks.
Five frontend workflow tests and the production build pass. A visibly labelled
UI fixture confirms Review change opens Activity and Review fix sends only the
deployment-preparation request (rejected by the fixture; no real app action).
Evidence: `.local/lifecycle-guards/OPS-LIFECYCLE-GUARD-001/codex-review/`.

## prepare_patch entry

Allowed phases: `needs_human`, `cancelled` only (`PREPARE_PATCH_ALLOWED_PHASES`).

Rejected without writes (patch, proposal binding, verification and
`baseline_error` preserved):

- Any `ACTIVE` phase (`queued`, `investigating`, `awaiting_approval`,
  `executing`, `verifying`, `preparing_patch`, `preparing_deployment`) or any
  other active work globally → `operation_busy`.
- `fix_proposed`, or any existing patch with `status tests_passed`
  (even from `needs_human` after a failed deployment build) →
  `fix_already_proposed`. Use explicit `prepare-deployment` to rebuild.
- `verified_fixed`, `mitigated` → `investigation_completed`. Start a new
  investigation for new work; history is never overwritten.
- Unknown phase → `investigation_not_eligible`.
- Missing diagnosis → `diagnosis_required`; missing source/profile →
  `patch_profile_required`; disconnected → `target_disconnected`.
- Diagnosis-time `source_revision` missing or != enrolled
  `source_revision`, or failed-patch source mismatch → `source_changed`.
- Existing patch bound to another investigation/target, or diagnosis without
  `observation.current.identity.container_id` → `diagnosis_obsolete`.
- Live `_observe` error/missing identity → `observation_unavailable`.
- Live `container_id`/`image_id`/`started_at` != diagnosis-time identity, or
  live image != enrolled `source_image_id` (when enrolled) → `target_changed`.
- Enrollment or investigation changed during collection →
  `preparation_state_changed`, without phase/job creation.

No model call decides entry. Failed-preparation retry (`needs_human`/`cancelled`
with failed patch, same investigation/target, unchanged source/identity) is
allowed and starts paid work in a background task.

## promote_deployment retry

`POST /api/v2/deployments/{proposal_id}/promote` remains the only explicit
retry after a verified correction whose baseline bookkeeping failed. It
requires no `ACTIVE` work (unpaginated) and a `verified` proposal bound to a
`verified_fixed` investigation. Success clears `baseline_error` and sets
`baseline_promoted`; failure preserves `verified_fixed` and its error. Repeated
`prepare_patch` can no longer overwrite `verified_fixed`, and new unrelated
investigations create new records without rewriting history. No generic
phase-reset endpoint exists.

## capture_snapshot

Blocked (same target only, unpaginated) when the target has `ACTIVE` work,
a transient same-target task, or any `proposed` proposal → `operation_busy`.
Unknown outcomes alone do not block capture (existing outcome-guard behavior).

Flow under the service lock:

1. Pre-check before collecting.
2. `_observe`, then executor preflight with `persist=False` (no bytes stored).
3. Final synchronous guard with no `await` before the store: re-read enrollment
   (`policy_changed` if changed) and re-check same-target pending/in-flight
   work. Rejection preserves existing baseline bytes and proposal bindings.

Other idle targets are never blocked by unrelated history.

## Startup recovery (idempotent, DB-only)

- Expires `pending` reconciliation reviews; `consumed` audits keep covering
  their exact content. Never deletes/expires `unknown`, never lifts a gate.
- Repairs every `ACTIVE` investigation via unpaginated scan (never 50/1000
  UI windows):
  - `awaiting_approval` with a matching never-dispatched `deploy_verified_patch`
    proposal, or `preparing_deployment` interrupted before dispatch, returns to
    `fix_proposed` only when the retained patch is `tests_passed` with a
    present `patch_id`, target/investigation/source bindings are consistent
    (legacy fields missing are skipped, present fields must match), no
    unresolved unknown exists for the target, and no proposal for the
    investigation shows dispatch (`executing`/`dispatching`/`verifying`/
    `rolling_back`/`executed`/`verified`/`verification_failed`/`rolled_back`/
    `unknown`, dispatch timestamps/identities, or `result.command_completed`).
    Otherwise stays `needs_human` with a specific reason plus the generic
    interrupted message.
  - All other `ACTIVE` phases go to `needs_human` with `interrupted`.
  - Rebuild never claims fresh tests: `patch.completed_at`/`status` unchanged,
    evidence/diagnosis/patch retained, explicit `prepare-deployment` required,
    existing machinery rebuilds/revalidates, new `build_artifact`/proposal hash
    created, fresh human approval required. Prior `proposed` records expire.
- Proposals: `executing`/`dispatching`/`verifying`/`rolling_back` → `unknown`;
  `proposed` → `expired`; `executed` whose investigation was `executing`/
  `verifying` at restart with no verification in either record → `unknown`
  (durable per-target gate). Completed `executed` with verification,
  `verified`/`verified_fixed` (even with `baseline_error`), and `mitigated`
  are preserved across repeated startups.
- Unknown proposal changes commit before active investigation repair. A matching
  complete verified deployment result is copied back to its interrupted active
  investigation before considering generic `needs_human` recovery.
- Starts only the read-only monitor task. Never launches Docker mutations,
  model calls, patch generation or rollback.

## Safety windows and races

- All safety decisions use `store.scan_investigations()` (unpaginated) or
  already-unpaginated proposal/unknown queries. UI snapshots keep
  `list_investigations(limit=50)`.
- Capture and promotion busy checks are unpaginated; `disconnect` cancels all
  same-target `ACTIVE` work beyond 50; `_kick` scheduling sees all rows.
- In-process async safety comes from the service lock plus no-`await` windows
  between final checks and durable stores. Direct external SQLite writers
  bypassing the service, and host/container changes during the final dispatch
  CLI itself, remain possible and are not claimed as atomic.
- Reconciliation coverage still binds exact unknown IDs plus content hashes; a
  later new/changed unknown blocks again. Restart/phase repair never silently
  covers new content.
