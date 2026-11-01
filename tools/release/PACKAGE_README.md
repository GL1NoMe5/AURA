# AURA — clean install package

This is a generated release document for this source package, not the
repository README. It lists only what this package contains and the
commands verified against a fresh extraction.
All paths below are relative to the package root. This template also
ships under `tools/release/` so the extracted source can be repackaged.

## Contents

- Windows launchers: `Start-AURA.cmd`, `Connect-Baseten.cmd`,
  plus the `.env.example` template.
- Backend sources, runtime helper, and the test helpers with static
  fixtures needed to collect the backend suite.
- Frontend sources, configs, lockfile, and derived portrait/audio assets.
- Locked dependency metadata (`backend/uv.lock`,
  `frontend/package-lock.json`) and third-party notices under
  `resources/`.
- Clean-start instructions: `docs/CLEAN_START.md`; release readiness:
  `docs/RELEASE_READINESS.md`.
- Current acceptance and review boundaries: `docs/FINAL_ACCEPTANCE.md`,
  `docs/REVIEW_DISPOSITION.md`, plus the test-protocol, verification,
  reconciliation and lifecycle documents in `docs/`.

Not included: virtual environments, `node_modules`, build output,
databases, keys, model weights, raw portrait sources, or private
generated workspaces. AURA and its portrait artwork are by Emir. Original code
is MIT-licensed; LICENSE, MEDIA_LICENSE.md and THIRD_PARTY_NOTICES.md describe
the separate terms for code, portrait and music.

## Supported commands

From the extracted folder, after `Start-AURA.cmd --setup`:

- Start: `Start-AURA.cmd`, then open <http://127.0.0.1:8000/>.
- Backend test collection (verified):
  `backend\.venv\Scripts\python.exe -m pytest backend/tests --collect-only -q`
- Full test execution was not repeated from this extraction. Collection
  verifies imports/discovery only; separate real acceptance harnesses need
  Docker and prepared lab fixtures.

The `tools/ops` acceptance and lab helpers are included with their static
source dependencies for review and test collection. They require explicitly
prepared local lab state, Docker images and fixture credentials; this ZIP
does not include that private state or automatically prepare/run those labs.
See `tools/ops/FINAL_ACCEPTANCE.md` and `tools/ops/ACCEPTANCE.md`.

The readiness record separates current archive validation from historical
checks. Read the sidecar validation report for the exact archive under review;
an earlier smoke test does not certify later source changes.

Packaging details: `tools/release/README.md`.
Resource provenance: `resources/sources.json`.
