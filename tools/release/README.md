# Release packager (OPS-RELEASE-002-R1)

`package.py` builds a reviewable local source ZIP from an explicit allowlist.
Nothing is published. Run it from the project root:

```powershell
python tools/release/package.py [--output <zip>]
```

Default output is `.local/release/OPS-RELEASE-002/aura-clean-<UTC>.zip`,
with two sidecars:

- `.zip.manifest.json` — package members, sizes and SHA-256 hashes, plus the
  archive hash. `package_bytes` is the archive size on disk;
  `total_bytes` is the uncompressed member sum. Renamed members carry
  their repo `source`.
- `.zip.checks.json` — forbidden-path, likely-secret, and Markdown-link
  report. Paths and rule names only; matched values are never printed
  or recorded. Binary files are listed as unscanned with an explicit
  classification, and reviewed exact-line exceptions are listed with
  value-free reasons.

## What is included

- Launch/config: `Start-AURA.cmd`, `Connect-Baseten.cmd`, `.env.example`,
  `docs/CLEAN_START.md`, `docs/RELEASE_READINESS.md`, and the package root
  `README.md` (mapped from `tools/release/PACKAGE_README.md`; the
  repository README stays in the repository).
- Backend/runtime sources: `backend/aura_*`, `backend/tests`,
  `backend/pyproject.toml`, `backend/uv.lock`, `tools/runtime/aura_runtime.py`,
  `tools/release/` itself, plus the `tools/ops` and `tools/preflight`
  helpers and static fixtures the packaged tests need for collection.
- The current final-acceptance and upstream-test helpers, lab seed scripts,
  pinned lab Dockerfiles, and guard/acceptance documentation. The owned
  test runner and protocol validator ship with the `backend/aura_ops` tree
  and are mandatory members.
- Frontend: source, tests, configs, lockfile, and derived
  `frontend/public/portrait/*` plus `frontend/public/audio/*` assets.
- Third-party notices: `resources/sources.json`, skill/pinned-app licenses,
  fixture provenance records, the Three.js license, and the psrdnoise MIT
  notice (its full text also ships inside the shader copied with
  `frontend/src`).

## What is excluded

`.local`, `.git`, `.agents`, virtual environments, `node_modules`, build
output, raw portrait sources (`assets/`), model weights
(`resources/models/`), credentials (every `.env` variant except the root
`.env.example` template, in any letter case), runtime databases, and
private generated workspaces. Symlinks, junctions, and reparse points are
rejected on every ancestor as well as on members, even when they point
back inside the root. Lexical `..`, absolute paths, and noncanonical
names fail the build. Missing allowlisted files and missing required
members fail the build too. Every local Markdown link must resolve to a
packaged member.

The secret scan is a packaging hygiene check, not a security audit: it
flags PEM blocks, provider-key tokens, quoted and unquoted assigned
secrets (including quoted JSON keys), and credentialed connection
strings. Unquoted bare code expressions (identifiers, dotted attributes)
and unquoted template references (values starting with `{` or `$`) are
not findings; quoted literals stay strict. Fixture exceptions apply only
to reviewed exact lines,
matched by content hash and recorded with value-free reasons. Original AURA
code is MIT-licensed. The archive includes LICENSE, MEDIA_LICENSE.md and
THIRD_PARTY_NOTICES.md; portrait and music rights are separate from the code.

The private post-guard refresh uses existing dependencies only: archive/hash
validation and test collection from a fresh extraction. This does not repeat
setup, runtime smoke or Docker acceptance, and does not package private lab
state. Historical verification remains labelled with its original version.
