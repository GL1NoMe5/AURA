# Test evidence protocol (OPS-TEST-PROTOCOL-001)

Structured pytest outcomes replace console-text regex at both patch
preparation and deployment validation. Logs stay diagnostic only.

## Report envelope and schema

- The AURA-owned runner (`backend/aura_ops/test_runner.py`, version 1) is
  mounted read-only from outside the candidate workspace; the candidate
  cannot edit it or choose the runner. Container command is fixed:
  `python /aura/test_runner.py --stage … --attempt … --nonce …`.
- The reporter prints exactly one envelope to stdout:
  `AURA_TEST_REPORT_BEGIN <nonce>`, one JSON line, `AURA_TEST_REPORT_END
  <nonce>`. Any missing, duplicate, truncated or nonce-mismatched envelope
  rejects the stage. Console text is never parsed for acceptance.
- Report (`protocol aura-test-report`, version 1) binds: runner SHA-256,
  stage (`before`/`after`/`original-suite`/`collect`), attempt (1–2),
  expected paths, enrolled profile hash, workspace manifest hash, per-run
  nonce, plus collected node IDs, per-phase outcomes with exact exception
  types from `pytest_runtest_makereport(excinfo)`, deselected/collection
  errors, and session completion/exit code.
- Transport bounds: report ≤256 KiB, ≤2000 collected nodes, node IDs ≤500
  chars, strict JSON (no NaN/Infinity or duplicate object keys), exact int/str types. The container
  exit code must equal the reported exit code.

## Gate rules

- `collect` (on unchanged baseline, original-suite paths): nonempty
  collection, no errors/skipped modules/deselection, exit 0. Collection-only evidence never
  counts as passing.
- `before`: same nonempty regression node set will be required of `after`;
  at least one call-phase `AssertionError` (builtins), no other failure
  classes, no setup/teardown/collection errors, no skip/xfail/deselection,
  exit 1.
- `after`: exact same node set as `before`, every setup/call/teardown
  passed, exit 0.
- `original-suite`: exact baseline inventory preserved, everything passed,
  exit 0.
- Preparation and deployment share `backend/aura_ops/test_protocol.py`;
  deployment re-validates stored reports and ignores cached booleans.
  The complete stored argv must equal the owned invocation, including both
  hashes and `/workspace`; extra/duplicate options reject. The report nonce
  must match that invocation. Boolean/float versions or attempts reject.
  Log-only candidates without reports are explicitly nondeployable
  (`deployment_protocol_missing`) until freshly validated.

## Secrets and legitimate upstream

- Structural IDs/hashes/node IDs are preserved exactly; only free-form
  report text and saved logs pass secret redaction.
- If enrolled upstream legitimately needs skips/xfails/deselection, that is
  reported as an incompatibility with this protocol. Gates are not relaxed
  to accommodate it.

## Prerequisites (observed, no image changes)

- Host checks used installed pytest 8.4.2 (project venv) for real local
  runner runs. Wonder's handoff did not use Docker/network; Codex's subsequent
  acceptance below used the existing Linux test image without network.
- The approved test image must provide pytest with stable hooks
  (`collection_finish`, `deselected`, `collectreport`,
  `runtest_makereport`, `sessionfinish`). The reporter uses new-style
  `wrapper` hooks on pytest ≥7 and `hookwrapper` below; read-only bind
  mounts and the existing isolation flags are unchanged. No new image
  package is required (runner is stdlib + pytest only).

## Codex acceptance — 2026-10-30

The external runner now explicitly adds the resolved application root to
`sys.path` before pytest/plugin discovery. A nested generated regression had
failed to import application code before this correction. Collection skips
are recorded as incomplete collection, including module-level skips that
would otherwise disappear from the inventory.

Filename literals matching an edited file remain conservatively restricted,
except direct relative `tmp_path` fixtures and imported Django
`SimpleUploadedFile` filename metadata. Direct source inspections still reject;
this narrow exemption is not general static data-flow analysis.

Independent focus: 254 passed / 1 existing Windows symlink-privilege skip.
After the filename correction, its focused set passed 84 / 1 skipped,
including 18 new positive/negative fixture cases (overlaps the first set).
The 17 new runner/protocol regressions failed before their fixes and passed
afterwards; fixture regressions initially had 6 failures / 12 negative passes.

Six disposable real-container stages passed through production `PatchEngine._test`
and the shared validators: tiny baseline collection, assertion failure before,
pass after, original suite; then 53-node collection and all 53 tests on an
independent 439-file copy of the installed corrected Linkding source. Linux
read-only source enforcement and writable temporary scratch were exercised.
All six owned containers were removed; preexisting container ID/image/StartedAt
fingerprints and original source remained unchanged. No provider calls, image
builds/pulls, target changes or deployment were performed. This validates the
test protocol, not a fresh AI repair or the entire release.

Evidence: `.local/test-protocol/OPS-TEST-PROTOCOL-001/codex-review/`, especially
`codex-5e46d1c03e3547699e6753d26fee5fe6/result.json`. The upstream-check helper
also uses collection plus the structured suite gate with the new return shape.
Changing the runner changes its hash: old evidence must not be relabelled as
current-run evidence or silently rewritten.

## Residual limits (no overclaim)

- Candidate tests share the pytest interpreter with the reporter, so a
  hostile test can read argv/env (nonce), write raw fds, resolve banned
  calls via `getattr`/indirection to dodge static AST checks, or terminate
  the process (`os._exit`) to suppress the real envelope. The nonce,
  read-only mount, exit agreement and duplicate detection block accidental
  collisions and direct named bypasses only.
- JSON, nonces, read-only mounting and AST denylists do not make arbitrary
  hostile Python trustworthy. Same-interpreter tampering and semantic test
  adequacy remain out of scope; independent runtime verification and human
  approval still decide deployment.
