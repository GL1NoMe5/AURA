# AURA release readiness

## Public source preparation, 2 November 2026

The public snapshot contains the runtime, locked dependencies, launchers, tests,
documentation and the prepared portrait/audio assets. Private runtime stores,
credentials, raw portrait sources, film project files, development skills and
model weights are excluded by an explicit file inventory.

Original AURA code is MIT-licensed. Separate portrait and soundtrack terms are
in the root MEDIA_LICENSE.md and THIRD_PARTY_NOTICES.md. The two-minute film
uses Cat Walk by Arulo under Mixkit's music license.

The README describes two different outcomes: automatic settings recovery in one
recorded Linkding run, and a separate code repair that required human correction
and approval. Neither establishes general autonomous repair reliability.

Publication checks cover the selected files, a heuristic credential scan,
local documentation/media links, README rendering and the release packager
(27 tests passed; one existing Windows symlink-privilege skip). No new paid
model trial, Docker intervention or 20-minute soak accompanies this README update.

Use `tools/release/publication.py` to reproduce the public source inventory and
ZIP. The check report and hashes are written under `.local/publication/20261102/`.
The earlier clean-folder runtime checks and engineering findings below remain
dated evidence, not newly repeated tests.

## Historical engineering records

The following entries retain their original status at the time of each run.
Their former licensing and publication prerequisites are superseded above.

## Current refresh — 2026-10-31

The new private archive is
.local/release/RECOVERY-20261031/aura-source.zip. Its manifest identifies exact
source bytes; checks and runtime validation are saved alongside it. Previous
archives below are historical checkpoints, not the current build.

A real automatic configuration recovery passed on an isolated Linkding target:
monitor detection, three Open 1026 calls, policy-authorized restoration, and
verified HTTP availability in 93.301 seconds. See [LIVE_RECOVERY.md](LIVE_RECOVERY.md).
The backend was unchanged by this trial. The frontend now exposes a separate
restore-settings permission and labels monitor-triggered checks correctly;
36 frontend tests and the production build passed. Browser review covered the
real result and then the changed UI using explicitly labelled read-only playback.

The full backend run produced 864 passes, 3 skips and one packaging-link failure:
it had loaded the packaging allowlist before the concurrent documentation edit
added LIVE_RECOVERY.md. A new packaging run with the final allowlist passed
27 tests with 1 existing skip. Runtime tests did not fail; the full suite was
not repeated. New private setup/smoke results describe only this archive and
must be read separately from the live Docker trial.

The portrait is attributed to Emir at the author's request. Publication still
needs the project's chosen license and final media/attribution review. No
publication or submission has taken place. The file-download code repair is
still human-assisted; a successful config restore does not change that claim.

## Historical release candidate - 2026-10-30

Development update 2026-10-31: patch candidate instructions and failure-focused
retry context changed after the UI-reviewed archive. That archive is a preserved
checkpoint and no longer matches current backend source. See
docs/PATCH_RETRY_CONTEXT.md in the development checkout. Rebuild and validate a
new release after accepting this change; do not reuse the old archive's runtime
claims for it.

The subsequent UI walkthrough is complete. Connecting an app now opens its form
directly; Check app opens a context/review form before the explicit paid submit.
Errors receive focus and retain the entered context. A deployment proposal shows
its bound patch diff directly, and historical diagnoses without findings render
without crashing. The tutorial explains when credits are used.

The refreshed private source package is
.local/release/UI-REVIEW-20261030/aura-source-ui-reviewed.zip. Its manifest and
checks identify the exact revision. Browser checks used an isolated, keyless
backend for watch-only enrollment and a clearly labelled, command-disabled
fixture of the previously verified human-assisted repair for approval/result
screens. No new repair or paid request is claimed. Frontend tests (34) and the
production build passed. Portrait, simulation, audio and backend runtime were
not changed. Temporary review servers were stopped.

The current source includes target management and the latest repair-contract
changes. Read the latest section of FINAL_ACCEPTANCE.md before using historical
repair claims: the new verified deployment is human-assisted. It does not prove
reliable autonomous repair. Candidate tests and operational reports are private
evidence, not bundled application defaults or preinstalled fixes for Linkding.

A fresh source ZIP and isolated clean-folder setup/runtime verification completed
under .local/release/FINAL-20261030/. Locked setup and production build succeeded;
34 frontend tests and 22 HTTP/assets/conflict/shutdown checks passed. No provider
key or enrolled target was present. Owned processes exited gracefully.

The first complete backend run in the extraction had 859 passed, 3 skipped and
2 failures: offline preflight tests incorrectly depended on developer-local Docker
and private fixture files. Both now create their own temporary preflight inputs;
all 40 tests in the affected two modules pass. No runtime code or test assertions
were relaxed. The complete suite was not repeated after these test-only fixes.
That earlier FINAL-20261030 archive differs from its smoke-tested copy only in
those test fixtures and release documentation. The newer UI-REVIEW-20261030
package additionally includes the three frontend source changes described above;
the old runtime smoke is not claimed as a new clean-folder UI walkthrough.

Manifest, scanner and validation sidecars identify the exact final artifact.
Publication still requires a project license and portrait provenance; this
candidate is for private local review, not a clean-machine certification.

## Current post-guard refresh — 2026-10-30

The private package now includes the accepted action, reconciliation,
verification, lifecycle and structured-test guard code. Current status and
remaining behavioral limits are recorded in `docs/FINAL_ACCEPTANCE.md` and
`docs/REVIEW_DISPOSITION.md`; the older unresolved-finding summary below is
historical, not the current disposition ledger.

Packaging adds the final-acceptance/upstream-check helpers, their static lab
dependencies, and the guard/acceptance documentation. The owned test runner
and shared protocol validator are required members. Three inert guard-test
markers plus the newly included seed helper's runtime credential-generation
expression have exact path/line-content hash exceptions with reasons. No
scanner regex, fixture values, broad exclusions or redaction tests were weakened.
Any edited or relocated reviewed line loses its exception.

The refresh evidence belongs under
`.local/final-acceptance/OPS-FINAL-ACCEPT-001/codex-review/release/`:
archive/member hashes, strict scan/link checks and backend test collection
from a fresh extraction using an existing interpreter. No new installation,
application/server start, Docker run or provider request is part of this
package verification. The R0 runtime smoke below cannot be reused as a smoke
of the changed backend/UI; a fresh-folder collection is not a clean-machine
runtime certification. All earlier archives/evidence are preserved.

Publication remains pending the AURA project license and portrait provenance;
this private ZIP grants no redistribution rights. The package includes selected
current guard/acceptance docs but excludes `.local` evidence, credentials,
runtime stores, raw portrait sources and model weights. A broader repository
publication review remains separate.

## Latest Codex review — 2026-10-29

R1 closes the original import-collection and quoted-secret/path findings.
Codex found one further completeness issue: the mapped README source was
absent from the extracted tree, breaking its real-tree package test and
repackaging. It now ships at both its template path and the root README;
the roundtrip test checks that the extraction is collectable by the packager.
The added PACKAGE_README.md is accepted as necessary to the assigned task.
The scanner's unsupported claim of human review was corrected to agent review.

Focused repository release tests: 31 passed, one real-symlink privilege skip;
deterministic mocked reparse tests pass. Candidate for the corrected package:
`.local/release/OPS-RELEASE-002-R1/codex/aura-clean-reviewed.zip`.
Original R0/R1 archives remain untouched. Runtime smoke is reused only for
byte-identical runtime/assets; this is not a fresh-machine certification.
The scanner remains heuristic, including unquoted-expression exemptions;
zero findings is not proof that every possible credential format is absent.

Publication is still blocked by unresolved runtime integrity findings from
independent review, project licensing and portrait provenance. In particular,
patch-test isolation, unknown-action reconciliation and complete verification
evidence need corrections before a final release build. No known live exploit
or actual secret disclosure was established. After backend fixes, rebuild and
review a new package; this ZIP is a local engineering checkpoint only.

**Codex review, 2026-10-29: package changes requested.** Original first-run logs and archive hashes were confirmed, but scanner/path validation and missing test helpers/documentation require OPS-RELEASE-002-R1. Review: `.local/release/OPS-RELEASE-002/codex-review.json`. The original facts below are historical results. R0 ZIP size is 10,424,915 bytes; 15,251,337 is the uncompressed sum.

## R1 update (2026-10-29)

The R1 package supersedes the R0 ZIP for review; the original ZIP,
manifest, and evidence are preserved unchanged.

- Secret scan: quoted JSON keys now match; broad substring and symbol
  exemptions are gone. Unquoted bare code expressions are skipped by
  syntax; fixture exceptions apply only to reviewed exact lines
  (content-hashed, value-free reasons recorded). All decodable text is
  scanned regardless of size; binaries are listed as unscanned with an
  explicit classification.
- Path validation: every `.env` variant is rejected except the root
  `.env.example` template (any letter case); links and reparse points
  are rejected on all ancestors; lexical `..`, absolute paths, and
  noncanonical names fail.
- Completeness: the packaged tests' `tools/ops` and `tools/preflight`
  helpers plus static fixtures ship explicitly. The package root
  README is a labelled package document, and this record ships so the
  clean-start reference resolves.
- Sizes: manifests carry both numbers as `package_bytes` (archive on
  disk) and `total_bytes` (uncompressed member sum).

### Launcher/server PID note (no rerun)

The R0 smoke record shows launcher PID 17284 while the uvicorn log
shows server process 20284: the venv `python.exe` created by uv is a
launcher shim (268,568 bytes vs 103,192 for the base interpreter) that
spawns the real interpreter as a child in the same process group. The
shutdown signal reached the group and the log shows lifespan shutdown
completed, so the graceful-shutdown evidence stands. Limit: the smoke
helper verified the launcher PID, the freed port, and the log lines,
not a full child-process-tree scan. No smoke rerun was performed for
R1; runtime and frontend asset bytes are verified identical to the
smoked package (see the R1 hash comparison), so the R0 first-run
evidence is reused as stated here.

### R1 package

- `.local/release/OPS-RELEASE-002-R1/aura-clean-r1.zip` with manifest
  and checks sidecars; the archive hash is recorded in the manifest
  sidecar.
- Fresh-extraction collection with the reused R0 clean interpreter:
  519 collected, 0 errors (repo baseline: 519 collected, 0 errors; the
  R0 package reproduces 359 collected with the same 10 helper-import
  errors Codex reported). Imports resolve from the extraction. Full
  test runs were not executed.

## Original verification (OPS-RELEASE-002)

## Verified facts

- `tools/release/package.py` builds an explicit-allowlist ZIP. Package
  `.local/release/OPS-RELEASE-002/aura-clean-20261029-123038.zip`:
  131 files, 15251337 bytes, SHA-256
  `1df695879e98fe25888462c7ed53679b10d7c052ed058746ebc92363dc5d6ae7`.
- Sidecar manifest re-verified with independent hardcoded rules:
  18 required members present, hashes match working-tree bytes, ZIP
  members equal the manifest, package hash matches.
- Packaging checks: 0 forbidden paths, 0 likely-secret files. Six
  first-pass candidates were classified as false positives (a code
  attribute reference, redaction-test fixtures using documented
  placeholder words, one self-test token built from fragments); the
  scanner rules were refined and the package rebuilt. Values never
  recorded. This is a hygiene check, not a security audit.
- Fresh extraction holds no `.env`, venv, `node_modules`, `dist`,
  `.local`, `assets/`, or `resources/models/`.
- Documented setup ran in the extracted copy with isolated caches:
  `uv sync --locked` (34 packages), `npm ci` (33 packages,
  0 vulnerabilities), `tsc -b && vite build` (53 modules, existing
  chunk-size warning only). `check --port 8015` passes.
- The clean interpreter and imports resolve inside the extracted copy;
  built `dist` portrait/audio bytes are identical to the packaged
  `public` sources.
- First run on 127.0.0.1:8015 with provider overrides stripped:
  20/20 smoke assertions pass. `/health` and `/api/v2/status` return
  200 with no targets, no investigations, `active_id: null`,
  `provider_ready: false`, and no demo worker. The built UI plus all
  four portrait/audio assets are served with matching hashes. The app
  created its own empty store under the clean copy.
- Port-conflict probe fails cleanly (`check --port 8015`, exit 3,
  "already in use") without touching the listener; the server stays up.
- Shutdown was graceful (CTRL_BREAK, uvicorn lifespan completed); no
  owned process remains and port 8015 is free again.
- `provider_requests=0`: no key in the child environment, no `.env` in
  the clean copy, `provider_ready=false` at runtime, and the adapter
  raises `missing_key` with 0 attempts when unready. No investigations
  were started.
- Release tests: 16 passed
  (`test_release.py`, `test_release_websocket.py`,
  `test_release_package.py`), one pre-existing TestClient-deprecation
  warning.
- I invoked no Docker, lab, acceptance, live-diagnosis, or soak
  commands; no container/network/volume was created, started,
  stopped, or recreated (the app's own read-only availability probe
  is its normal startup behavior). The installed corrected Linkding
  image is preserved.
- No first-run/packaging bug was reproduced, so `aura_runtime.py`,
  `Start-AURA.cmd`, and the existing release tests are unchanged.

## Reused vs downloaded

- Reused from this PC: `uv` 0.12.13, Node.js 24.19 / npm 11.17,
  system CPython 3.10.11 (no interpreter download; the isolated
  `uv-python` dir was never created), and the OS itself.
- Downloaded into isolated task caches from the registries:
  `caches/uv` (42 MB), `caches/npm` (33 MB). All installs came from
  the committed lockfiles (`backend/uv.lock`,
  `frontend/package-lock.json`).
- Full transcript: `.local/release/OPS-RELEASE-002/install.log`.

## Remaining blockers (publication)

1. No project LICENSE file exists; a license decision plus the file
   itself are still required. The packager ships source notices only
   and invents no license.
2. Portrait source rights/provenance are undocumented (see
   `assets/portrait/README.md`); the package ships derived
   portrait/audio buffers built from that source.
3. The package carries `README.md` plus `docs/CLEAN_START.md` only;
   the rest of `docs/` (continuation, evidence-linked ops docs) stays
   in the repository by design and needs a publication decision.

## Still requires a real clean machine

This was a fresh folder on the development Windows PC, not proof for
other environments. Unverified: other operating systems, machines
without `uv`/Node/Python preinstalled, offline installs, other
Python/Node versions, first runs with Docker targets attached, and
any performance claim beyond "it starts and serves".
