# Patch retry contract (OPS-PATCH-RELIABILITY-001)

Offline contract for the bounded two-candidate repair generator in
`backend/aura_ops/patches.py`. Runtime model remains Baseten
`meta-models/muse-spark-open-1026`; this document does not prove model
accuracy and no live trial is implied.

## Contracts

| Attempt | Condition | Output contract (exact JSON keys) |
|---|---|---|
| 1 | always | `Candidate`: `summary`, `edits`, `regression_test` |
| 2 | attempt 1 `before` **and** `after` regression gates both passed | `EditsOnlyCandidate`: `summary`, `edits` only |
| 2 | otherwise (no reproducer passed both stages) | full `Candidate` again |

Both contracts are strict (`extra="forbid"`), bounded (`summary` ≤1500 chars,
1–3 edits, each `old`/`new` ≤16000 chars), and keep the allowed-path enum on
`edits[].path`, exact unique source anchors, and one combined edit per file.
Missing edits, empty edits, incomplete edits and duplicate edits for one
file stay rejected (`patch_schema_invalid` / `patch_duplicate_edit`), as do
forbidden paths, non-unique anchors and invalid Python.

## Frozen reproducer

- The freeze is set in the stage loop at the moment attempt 1's `after`
  gate passes with its `1-before` gate already passed. Baseline-collection
  failure, a failing `before`, or a failing `after` never freezes.
- Server state keeps the exact attempt-1 regression bytes. Candidate 2's
  request binds them as `packet["frozen_regression_test"] =
  {"path", "sha256", "content"}` with `packet["retry_contract"] =
  "edits_only_frozen_regression"` (otherwise `"full_candidate"` and `null`).
  `previous_candidate` still shows which validated code produced the failure.
- Candidate 2 cannot supply test content: any `regression_test`,
  `test_content`, `regression` or other extra key is `extra_forbidden`.
  The engine builds the validated candidate as
  `Candidate(summary, edits, regression_test=frozen_text)` and revalidates
  anchors, syntax, test AST and escape guards before any run.
- Candidate 2 gets fresh `attempt-2-*` workspaces and freshly runs
  collection, `before`, `after` and `original-suite`. Attempt-1 reports are
  never reused. A failing fresh stage is recorded in
  `attempts[1].tests` with its actual stage name and diagnostic feedback;
  it is not discarded, and no passing record is produced from it.

## Malformed-output diagnostics

Rejected replies record only bounded metadata, in the attempt outcome,
retry feedback and the `provider_output_rejected` event:

- `output_diagnostic.category`: one of `empty_response`, `json_invalid`,
  `schema_invalid`.
- `output_diagnostic.json_line` / `json_column`: integer JSON position
  when the reply fails JSON parsing, else `null`.
- `output_diagnostic.response_chars` and `response_sha256`: size and hash
  so Codex can correlate a rejected reply without storing it.
- `validation_issues`: at most 8 `{type, field}` pairs; field parts are
  kept only when they are known contract fields, otherwise `response`.

Raw model output, model values, arbitrary output field names and
credentials never enter these structures, persisted artifacts, or retry
feedback. Executable candidate content that redaction would alter is
still rejected explicitly (`patch_candidate_redaction_conflict`).

## Bounds that did not change

- At most 2 candidates; `PATCH_MAX_TOKENS = 16384` and
  `PATCH_REASONING_EFFORT = "medium"` on patch calls; evidence packet
  ≤160000 chars; fixed runtime model check (`requested_model == MODEL`,
  `finish_reason == "stop"`); adapter attempt deadline and default
  diagnosis calls unchanged (adapter is outside this task's scope).
- No model loops beyond the two candidates, no fallback providers, no
  execution tools, and no acceptance bypass: deployment still requires
  all gates plus exact human approval, and original sources/tests are
  re-verified unchanged after validation.

## Limitations

Verified offline with fake adapters/runners and temporary stores only
(`provider_requests=0`). This shows the retry cannot rewrite a proven
reproducer and that malformed replies are diagnosable safely; it does
not show the live model will produce a correct repair.
