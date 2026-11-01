# Target management: editing and removing a connection

This document describes OPS-TARGETS-001 and its Codex UI integration.
My apps now supports editing/removing paused connections and a setup
summary. It is not an automatic repair-environment setup wizard: test
and install profiles still need an administrator-prepared connection file.

All examples are local API calls. Removing a registration never removes
a container, volume, image, source tree, workspace or evidence artifact.

## Setting up an app for repairs

The quick connection form lets AURA watch an app and investigate its logs and
resource use. It can propose a restart; execution still needs permission. It
does not prepare the code, tests or deployment environment for you.

Open **My apps > Settings > What's set up** to see the missing setup for each
tool. Ask the person who maintains the app to supply these items:

| Tool | Required setup |
| --- | --- |
| Restore working settings | The app's Compose file, project and service; a saved healthy working version. Source-bound apps also need a reviewed startup command. |
| Test code fixes | Local source matching an exact clean Git commit or verified manifest; an existing test image pinned by ID with dependencies already installed; original test paths and applicable framework settings. |
| Install a tested fix | All the above, the Docker image matching the source, its source-code location, a reviewed launch without database migrations, and operation checks with expected byte counts and content hashes. |

1. Connect the app, then **Pause watching**. This leaves the app running.
2. Open **Edit connection > Repair setup**. Enter Compose/source details or
   **Load setup file**. The file must use this connection's ID, container name
   and Docker context; the ID is shown in Repair setup.
3. Review the loaded **Test and install profile**, then save. Loading a file
   alone makes no change. Saving never grants automatic actions.
4. Choose **Resume watching**. Once the app is healthy, **Save working
   settings** for configuration recovery and deployment. A health probe proves
   current availability, not that all user operations work.
5. Use **Check app**, or add a symptom in **Activity** before starting a check.
   When source evidence supports a repair, **Test a fix** runs in an isolated
   copy. Passing tests makes a candidate available for review; it does not
   install the code. Review the exact change before allowing installation.

Test images and dependency preparation are administrator work; AURA's test
containers run without network access. It does not guess safe deployment
commands or automatically provision a repair environment for arbitrary apps.
Connection files contain credential *names*, never their values. Store values
locally in ignored `.local/ops/credentials.env` under `AURA_TARGET_...` names;
never put provider keys in a profile. See `TargetSpec`, `PatchProfile` and
`DownloadCheck` in `backend/aura_ops/models.py` for the strict schema.

After an edit, previous results stay in Activity but a new baseline and fresh
investigation are required. After a recovery action, the configured waiting
period is at least ten minutes. If an earlier proposal was blocked by that
period, run a **new check** once it ends; old consent is never revived.

## Target view and `connection_revision`

Every target view contains the enrolled `TargetSpec` fields plus
server-owned metadata. The same view shape is returned by:

- `GET /api/v2/targets`
- `GET /api/v2/targets/{target_id}`
- `GET /api/v2/status` (inside `targets`)
- the initial `WS /ws/ops` snapshot (inside `payload.targets`)
- `POST /api/v2/targets` (enroll), `PUT /api/v2/targets/{target_id}`,
  and the policy endpoint

```json
{
  "target_id": "linkding",
  "name": "Linkding",
  "docker_context": "default",
  "container_name": "linkding",
  "probes": [{"url": "http://127.0.0.1:9090/"}],
  "monitoring": false,
  "policy": {"automatic_actions": [], "auto_investigate": false,
             "restart_cooldown_seconds": 600},
  "connection_revision": 0,
  "created_at": "2026-10-30T00:00:00+00:00",
  "updated_at": "2026-10-30T00:00:00+00:00",
  "configuration": { "...": "see below" },
  "observation": null,
  "reconciliation_required": false,
  "unresolved_unknown_ids": []
}
```

`connection_revision` is a server-owned nonnegative integer.

- Records enrolled before this field existed are legacy records and read
  as revision `0`.
- It is not a `TargetSpec` field. Sending it inside `spec` is rejected
  by strict validation and it is never included in spec or proposal
  hashes, so old serialized specs keep their previous hashes.
- It increments by one only for an *effective* profile update through
  `PUT`. Telemetry samples, policy changes through the policy endpoint,
  disconnect/reconnect and no-op updates do not increment it.
- Investigations created after this change store the revision they were
  created under. Legacy investigations without the field mean revision
  `0` and remain valid only while the target is still at revision `0`.

## Update a disconnected connection

```
PUT /api/v2/targets/{target_id}
Content-Type: application/json

{
  "expected_revision": 0,
  "spec": { "...complete TargetSpec..." }
}
```

`expected_revision` must be a nonnegative integer. JSON `true`/`false`
are rejected. The body is strict: unknown or missing fields fail
validation (`422`) and write nothing.

Rules, all checked before any write and again atomically at commit:

- The target must exist (`target_not_found`) and
  `expected_revision` must equal its current revision
  (`target_revision_changed`). The commit also compares the complete
  current target record, not only the revision number, so a policy,
  reconnect or other write that lands while source validation is being
  awaited makes the update fail instead of overwriting it.
- The current connection must be disconnected and the submitted spec
  must keep `monitoring: false` (`target_must_be_disconnected`).
  Editing never reconnects a target.
- `spec.target_id`, `spec.docker_context` and `spec.container_name`
  must match the existing registration (`target_identity_immutable`).
  Changing the physical connection requires a separate new enrollment.
- Name, probes, Compose profile, source profile, patch/test profile,
  deployment root/launch and recovery checks may be replaced.
- Submitted automation is rejected, not silently stripped: a spec with
  `policy.auto_investigate: true` or a non-empty
  `policy.automatic_actions` fails with `enrollment_observe_only`.
  A successful update persists an observe-only policy
  (`automatic_actions: []`, `auto_investigate: false`) and retains the
  previous `restart_cooldown_seconds` even if the submitted policy
  named the `600` default. Granting automation stays with
  `POST /api/v2/targets/{target_id}/policy` after an explicit reconnect.
- If a source is supplied, the backend validates it with the existing
  `SourceIndex` checks and computes `source_manifest_sha256`
  server-side. A caller-claimed hash is never trusted: a mismatching
  claim fails validation, and with no claim the computed hash is what
  gets persisted. Failed validation writes nothing. No container
  collection or command runs for a profile edit.
- Active work for the same target blocks the edit (`operation_busy`):
  a live task, or durable investigations in `queued`, `investigating`,
  `awaiting_approval`, `executing`, `verifying`, `preparing_patch` or
  `preparing_deployment`, proposals in `executing`, `dispatching`,
  `verifying` or `rolling_back`, or a `proposed` proposal whose
  investigation is genuinely `awaiting_approval`. An unresolved
  `unknown` outcome blocks with `target_reconciliation_required`.
  These checks scan the full durable history, not a 50/1000-row window,
  and an idle unrelated target never blocks the edit.
- A restart cooldown does not block an edit, and the
  `lastrestart.<target_id>` marker is preserved exactly.

An *effective* update (the normalized submitted spec differs from the
current spec) commits, in one store transaction:

- the new spec with `connection_revision` incremented by one,
- an archive of the prior spec and prior snapshot metadata under
  `target_archive.<target_id>.<old_revision>`,
- removal of `snapshot.<target_id>` as the current baseline authority
  (the archived copy remains the historical record),
- removal of the current `verified_deployment` marker,
- expiry of stale, never-dispatched orphan proposals (a genuinely
  pending approval blocks the update instead of being expired),
- an audit event (`target_updated`) published to subscribers.

The service also clears the in-memory observation, history cache and
bad streak for the target. Historical investigations, diagnoses,
proposals, test results, diffs, verifications, promotions and artifacts
are kept unchanged. An old success is never rewritten as a newly
validated result.

A *true no-op* (the normalized spec equals the current spec, including
the persisted observe-only policy and retained cooldown) keeps the
revision, the cached baseline and `verified_deployment`, and writes no
archive.

Success returns the current target view including the new revision.

## Remove a registration

```
DELETE /api/v2/targets/{target_id}
Content-Type: application/json

{ "expected_revision": 1 }
```

The same revision, identity-free, disconnected, busy and unknown gates
as update apply, with two additions:

- If the effective disruption cooldown is still active
  (`lastrestart.<target_id>` younger than
  `max(600, policy.restart_cooldown_seconds)`), removal fails with
  `409 restart_cooldown`. Removing and re-enrolling cannot be used to
  erase a waiting period; retired IDs cannot be re-enrolled at all
  (below).

On success the transaction deletes only the target row, archives the
prior spec/snapshot like an update does, expires stale orphan
proposals, writes an audit event (`target_removed`), and leaves a
durable tombstone:

```json
{
  "target_id": "linkding",
  "connection_revision": 1,
  "removed_at": "2026-10-30T00:00:00+00:00",
  "audit_event_seq": 42,
  "archive_key": "target_archive.linkding.1"
}
```

Response:

```json
{"target_id": "linkding", "removed": true,
 "history_retained": true, "container_stopped": false}
```

After removal:

- `GET /api/v2/targets/{target_id}`, policy, reconnect, disconnect,
  update and a repeated delete fail as unregistered
  (`target_not_found`, `404`). A repeated delete therefore returns
  `404`, which is the documented absent-registration behaviour.
- Investigations, proposals and artifacts remain readable by their
  original investigation/proposal/artifact IDs, including after a
  store reopen. Nothing on disk outside the target row is deleted.
- The tombstone survives restart. Enrolling the same ID again fails
  with `target_id_retired` (`409`), forever. Tombstones do not count
  toward the three active registrations, so removing one of three
  targets frees a slot for a fourth, different application.
- Stale policy/reconnect/disconnect writes for a removed ID fail with
  `target_not_found` and never resurrect the record.

## Enrollment guards

`POST /api/v2/targets` is create-only, committed in one transaction and
rechecked after any source validation await:

- an existing ID fails with `target_already_enrolled` (`409`);
- a retired ID fails with `target_id_retired` (`409`);
- a second live registration with the same exact
  `(docker_context, container_name)` pair fails with
  `target_already_enrolled` (`409`), even under a different target ID.
  This is an exact string pair check; it does not claim to resolve
  Docker context aliases to a globally unique daemon;
- the three-registration limit still applies (`target_limit`), and two
  simultaneous enrollments cannot overwrite each other or exceed it.

## Old diagnoses cannot become current again (A -> B -> A)

Editing a profile from A to B and back to A increments the revision
twice (`0 -> 1 -> 2`), even though the final spec is byte-identical to
the original. Work bound to generation `0` therefore stays historical:

- Patch preparation, deployment preparation, proposal approval
  (`decide`), action execution and deployment promotion/retry reject a
  mismatched investigation generation with
  `target_configuration_changed` (`409`) before any model or Docker
  call. The guards live in the service and in the direct
  `ActionExecutor`/`DeploymentManager` entry points, and are rechecked
  after awaited preflights, immediately before dispatch. The proposal
  itself is never rewritten to fabricate a new generation.
- Startup repair uses the same check: an interrupted generation-0 fix
  is not rebuilt as `fix_proposed` after an edit; it becomes
  `needs_human` with its evidence intact, and unknown gates are
  preserved.
- A new investigation started after the edits binds the current
  revision (`2` above) and works normally. Never-edited legacy targets
  at generation `0` keep their existing behaviour.

## In-flight observations

The monitor captures the exact target record (identity plus revision)
before awaiting collection and rechecks it on return. If the profile
was edited or the registration removed in between, the late result is
discarded before any observation cache, history, bad-streak, event or
automatic-investigation write, and the monitor continues with the other
targets. Read-only collection used by verification and reconciliation
keeps its existing behaviour.

## Read-only configuration summary

Each target view carries a `configuration` object. For a minimal
connection (probes and a container only) it is exactly:

```json
{
  "monitoring": {"configured": true, "missing": []},
  "restart": {"configured": true, "missing": []},
  "configuration_recovery": {"configured": false,
      "missing": ["compose_profile", "known_good_snapshot"]},
  "code_fix": {"configured": false,
      "missing": ["verified_source", "patch_profile"]},
  "deployment": {"configured": false,
      "missing": ["compose_profile", "known_good_snapshot", "verified_source",
                  "source_image", "patch_profile", "deployment_root", "deployment_launch",
                  "recovery_checks"]}
}
```

Meanings, computed only from enrolled fields and whether a stored
baseline with a Compose profile exists:

| Section | Configured when |
| --- | --- |
| `monitoring` | at least one probe is enrolled (`probes`) |
| `restart` | a container identity (`docker_context` + `container_name`) is enrolled |
| `configuration_recovery` | Compose profile and known-good snapshot exist; source-bound targets also have `deployment_launch` |
| `code_fix` | verified source (`source_dir` + `source_revision` + server-bound `source_manifest_sha256`) and a patch profile are enrolled |
| `deployment` | all of: Compose profile, known-good snapshot, verified source, matching `source_image_id`, patch profile, `deployment_root`, `deployment_launch`, non-empty `recovery_checks` |

Missing tokens, in order: `probes`, `container_identity`,
`compose_profile`, `known_good_snapshot`, `verified_source`, `source_image`,
`patch_profile`, `deployment_root`, `deployment_launch`,
`recovery_checks`.

This object describes **setup only**. It never describes permission
(`monitoring` enrolment is not automation permission), health, a
successful test, or the ability to execute anything now. Computing it
runs no Docker, model or file preflight, creates no proposal, captures
no baseline and has no side effects.

## Errors

| Code | Status | When |
| --- | --- | --- |
| `target_not_found` | 404 | unknown or removed target ID |
| `target_already_enrolled` | 409 | enroll with an existing ID or duplicate `(docker_context, container_name)` |
| `target_id_retired` | 409 | enroll (or resurrect) a removed ID |
| `target_limit` | 409 | a fourth live registration |
| `enrollment_observe_only` | 400 | enroll/update tries to grant automation implicitly |
| `target_must_be_disconnected` | 409 | update/remove on a connected target, or update spec enables monitoring |
| `target_identity_immutable` | 409 | update changes `target_id`, `docker_context` or `container_name` |
| `target_revision_changed` | 409 | stale or non-integer expected revision, or the record changed during validation |
| `operation_busy` | 409 | live/durable active work or a genuinely pending approval for the same target |
| `target_reconciliation_required` | 409 | an unresolved unknown outcome gates the target |
| `restart_cooldown` | 409 | removal during the effective disruption cooldown |
| `target_configuration_changed` | 409 | old-generation diagnosis/patch/proposal/promotion work |
| `origin_forbidden` | 403 | `PUT`/`DELETE` from a disallowed browser origin (same check as other mutations) |
| source errors (`source_missing`, `source_unverified`, `source_revision_mismatch`, `source_manifest_changed`, ...) | 409 | supplied source fails `SourceIndex` validation; nothing is written |

Malformed bodies (missing/extra fields, a boolean revision, an invalid
spec) fail FastAPI validation with `422` and write nothing.

## Implemented UI

The existing glass workspace contains the connection controls:

- My apps > Settings > Pause watching, then Edit connection or Remove from AURA.
- Basic editing shows name and primary check address. Additional probes,
  credential references and repair profiles are preserved. Repair setup
  reveals Compose/source fields and explicitly staged connection-file loading;
  imported test/install/check profiles are reviewable before saving.
- Both editor and removal confirmation capture the displayed revision.
  A later update disables their action; a rejected request refreshes current
  state without replaying the mutation.
- What's set up lists the five setup categories separately from permissions.
- Activity > All apps, including removed retains recent historical checks.
  Results from older connection generations have no mutation buttons.
  Investigation-by-ID and artifact APIs keep the full retained history;
  the existing status snapshot still limits the UI's recent-history window.
- A new quick-form registration gets a unique ID even after removal/reload.
  Import remains available when all three slots are occupied, and an existing
  ID directs the user to its connection editor rather than overwriting it.
- Working version saved is visible again; Details exposes verification
  measurements without adding a permanent dashboard panel.

UI contract maintained by this implementation:

- Show `connection_revision` as an opaque version: send back the exact
  revision the form was loaded with, and on
  `target_revision_changed` reload the target and ask the user to
  review instead of auto-retrying.
- Only offer edit/remove for a disconnected target; explain that
  `target_must_be_disconnected` means "disconnect first" and that
  editing never reconnects or grants automation.
- Make identity fields (`target_id`, `docker_context`,
  `container_name`) read-only in the edit form and explain that a
  different physical connection is a new enrollment; a removed ID can
  never be reused (`target_id_retired`).
- Removal copy must say only AURA's registration is removed, history is
  retained, and the container is not stopped
  (`container_stopped: false` is the backend guarantee, not UI text to
  invent). Surface `restart_cooldown` as "wait, then retry".
- Render `configuration` as setup checklists using the `missing`
  tokens; never label a section as permission, health, readiness or
  "can run now".
- After a successful edit, expect the baseline and current
  `verified_deployment` to be gone and the observation cache cleared;
  prompt for a fresh snapshot/reconnect through the existing flows.

## Residual limits

- Exact `(docker_context, container_name)` matching does not prove two
  different context strings reach different daemons; alias resolution
  is out of scope.
- The store transaction makes AURA's own writers atomic. An external
  writer editing the SQLite database or host files directly during a
  transition is outside this guarantee.
- This contract alone does not build the missing setup wizard, invent
  test profiles or provenance, or choose launch commands for the user.
