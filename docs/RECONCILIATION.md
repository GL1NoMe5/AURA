# Operator reconciliation of unknown outcomes (OPS-RECONCILE-001)

Backend contract for the minimal Codex-owned review UI. Reconciliation acknowledges
**uncertainty** after a human inspects the actual target; it never proves the old
command succeeded, availability recovered, or a defect was fixed. No model can
approve it. Collection and confirmation never call a model.

## Flow

1. Target/status reads expose the gate (authoritative store query, independent of
   investigation pagination):
   - `GET /api/v2/targets`, `GET /api/v2/targets/{id}`, `GET /api/v2/status`
     (via `targets[]`) now include additive fields:
     - `reconciliation_required: bool`
     - `unresolved_unknown_ids: string[]`
2. Operator collects a read-only review (does not unlock anything):
   - `POST /api/v2/targets/{id}/reconciliations` → `201`, review body.
   - `GET /api/v2/targets/{id}/reconciliations/{review_id}` → inspect it.
3. Operator inspects the displayed target state, then confirms the exact record:
   - `POST .../{review_id}/confirm` with `{review_hash, attestation, operator_note}`.
   - Success reports `result: "outcome_reconciled"` plus the live remaining gate.
     It never says verified/mitigated/fixed and never moves an investigation to
     success.

## Request schemas (strict, extra fields rejected)

Collect: no body. Confirm body:

```json
{
  "review_hash": "<64 hex chars from the review>",
  "attestation": "I have inspected the displayed target state and confirm the previous operation has ended.",
  "operator_note": "<1..500 chars, what was inspected>"
}
```

The attestation sentence must match exactly. No caller-supplied evidence,
container-state hashes, allowed actions, command strings or success status exist.

## Review content (all redacted, bounded)

- `review_id`, `target_id`, `status: pending`, `created_at`, `expires_at` (+300 s).
- `target_created_at`, full enrolled `spec`, `spec_hash`, `policy_hash`.
- `docker_context`, `container_name`.
- `identity` (`container_id`, `image_id`, `image_ref`, `started_at`),
  `runtime` (`running`, `status`), selected `config`
  (`memory_limit_bytes`, `published_ports`, `server_port`),
  timestamped `probes` (`url`, `ok`, `status_code` per enrolled probe),
  `healthy` boolean, two observation timestamps.
- `unknowns`: exact set of ALL unresolved unknowns for the target as
  `{proposal_id, action, content_hash}`.
- `notices.no_proof` and `notices.observe_only` (display both before confirm).
- `review_hash`: binding over review/target/version/endpoint/state/unknowns/expiry.

Raw environment/secrets and complete Docker inspect output are never stored.
Operator notes are redacted before durable storage and event publication.

Codex review tightened completeness: selected config keys must be present, the
configured memory limit must be a nonnegative integer (0 means no configured
container limit), health must be an actual boolean, and running-container memory
usage/limit measurements must be finite and nonnegative. Partial collection is
rejected. Capture also rejects target/unknown changes during its observations.
Active work is checked without pagination, includes orphan in-flight proposals,
and is checked again inside the confirmation transaction across store instances.

## Stability and allowlist rules

Confirmation requires two fresh observations agreeing on identity, runtime
(`running`/`status`), selected config, probe `url`/`ok`/`status_code` and the
health boolean. Volatile data (timestamps, CPU/memory samples, log tails,
durations, restart counts, exit codes) is never compared. Known unhealthy HTTP
is valid evidence, not missing data. The current image must equal an exact
image ID already bound to the reviewed operations (`expected`/`base`/`candidate`
or recorded result identities); anything else stays blocked for new enrollment.

## State changes on confirmation (one SQLite transaction)

- Review → `consumed` with bound confirmation; durable `reconciliation_confirmed`
  event (same transaction, compare-and-set on current target + exact unknown set).
- Old `proposed` proposals for this target → `expired`. Original `unknown`
  records keep `status=unknown` with error, dispatch markers and history intact;
  the gate excludes only content-hash-matched records.
- This target's policy → observe-only (`auto_investigate: false`,
  `automatic_actions: []`, cooldown seconds preserved). Cooldown and baseline
  unchanged. Other targets untouched.
- A new or content-changed unknown blocks again. A consumed confirmation replays
  its saved result idempotently **with the live remaining gate** — it never
  dismisses a later unknown.

## Rejection codes (gate stays closed)

`reconciliation_not_required`, `reconciliation_active_operation`,
`reconciliation_collection_timeout`, `review_expired`, `review_hash_mismatch`,
`review_not_found` (also on target/review mismatch),
`reconciliation_state_changed` (unknown set, target, spec or policy moved),
`reconciliation_probes_changed`, `reconciliation_image_unrecognized`,
`attestation_invalid`, `invalid_operator_note`, plus reused `identity_missing`,
`observation_incomplete`, `target_changed`, `config_changed`, Docker `docker_*`.

## Residual limits (show in UI, do not overclaim)

The UI entry is **Review app state**, in the existing Review tab. It shows current
reachability/process state, keeps technical evidence in a disclosure, and requires
an explicit checkbox and note. Expiry or a changed unknown-ID set disables the
old form; all server-side bindings are still checked at confirmation. The result
is **Review saved**, never a repair claim. Reading the state uses no model calls.

- Snapshot stability DOES NOT prove the earlier Docker command stopped; the
  operator attestation is a trusted local assertion, not a machine-verified fact.
- Docker can change externally between the last check and the SQLite commit;
  the commit is atomic over local records only, not over Docker.
- Reviews expire after five minutes and on every restart; confirmed audit
  records survive restart. No timer or healthy probe substitutes for attestation.
- Reconciliation only permits NEW investigations/proposals under ordinary
  approval/policy checks; old actions are never retried or reapproved.
