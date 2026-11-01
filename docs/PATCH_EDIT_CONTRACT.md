# Patch edit contract

Independent exact anchors are resolved against original verified source. Up to
three input edits can share a file. Overlap, ambiguity, dependent anchors and
forbidden paths are rejected. Sorted disjoint edits become one bounded edit per
file for persistence/deployment. Final Python syntax is checked; CRLF is retained.
Raw and normalized edits both undergo credential checks.

Code differs from prose: exact configured secrets are rejected across all text;
string and comment tokens retain heuristic redaction checks. Identifier names
alone are not credentials. This is not a comprehensive secret detector: unknown
plain credential literals cannot reliably be distinguished from fixture data.
Malformed fragments fall back to conservative text checks. Logs/prose keep their
original redaction. Reference code is preserved only when code checks allow it.
Rejection diagnostics expose component/index, never matching values.

inspect remains prohibited, now explicitly stated in the prompt. No new test
framework privileges, provider calls or approval bypasses. Frozen test semantics
and before/after/original-suite gates remain mandatory. Offline tests do not prove
live repair accuracy.
