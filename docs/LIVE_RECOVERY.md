# Live configuration recovery — 31 October 2026

One end-to-end run passed on a disposable Linkding instance using the current
production controller, Docker adapter and Baseten Open 1026. The monitor started
the investigation; no person approved or edited the model's decision during it.
The operator had first enrolled the app, saved healthy settings and explicitly
allowed automatic restoration. This is a configuration recovery, not a code fix.

## Measured result

| Measurement | Result |
| --- | --- |
| Fault | Test harness changed the app's internal port from 9090 to 9091; the gateway still used 9090 |
| Detection | Sustained failed HTTP probes triggered the ordinary monitor |
| Model | `meta-models/muse-spark-open-1026` through Baseten |
| Calls | 3 completions, 3 HTTP attempts; runtime and configuration roles, then synthesis |
| Provider usage | 19,970 input + 8,248 output = 28,218 total tokens |
| Action | One `restore_config_snapshot`, authorized by the saved target policy |
| Result | Port returned to 9090 on the same image; health and authenticated bookmarks API returned HTTP 200 |
| Verification | 18 complete observations, zero incomplete observations; availability verified |
| Wall time | 93.301 seconds from starting the fault injection to the final result |
| Isolation | All temporary resources removed; registered Linkding and Healthchecks identities, images and start times unchanged |

Time includes fault injection, detection, model requests, Docker recreation and
verification. It is not model latency or a repeatability average. Usage is the
provider's token report, not confirmed dollar billing. The verification samples
span 44.696 seconds by their recorded timestamps; this is not a continuous probe.

The model received observed telemetry and the healthy baseline, without the fault
name, expected action or reference fix. Four healthy observations before injection
produced no investigation or paid request. Source analysis was not enrolled for
this configuration-only target, so there was no artificial source-agent call.

## What this supports

AURA can recover this demonstrated configuration fault autonomously within a
previously authorized policy. It does more than restart: it returns the app to
saved settings, then checks the actual HTTP operations. The interface reports
“App responding again”; `root_cause_fixed` remains false because no code repair
was tested. This single success does not establish accuracy across arbitrary
apps, incidents or repeated runs.

The diagnosis prose incorrectly described the policy as requiring a human
approval. The executor used the real stored policy, not that prose: the recorded
proposal has `automatic=true` and `authorization=policy`. The reasoning text is
therefore not an authority for permissions or a substitute for the audit record.

The separate October 30 file-download repair remains human-assisted: a generated
candidate needed a Codex correction before verified deployment. See
[the acceptance ledger](FINAL_ACCEPTANCE.md).

## Local evidence

Private evidence is preserved under
`.local/live-recovery-20261031/recovery-1031-a/`: `result.json` contains the
observations, calls, proposal, gates and cleanup record; `result-ui.png` is the
actual completed run in the application. The runner is
`.local/live-recovery-20261031/run.py` and is not an application dependency.

SHA-256 of the completed result:
`a60cf907dd602701259687f06adbeaafe8234b8a8bfd38a5f89237c4d4662dcd`.

After the run, the UI gained an explicit “Restore saved settings” opt-in beside
the restart switch. It remains off by default and unavailable until the server
reports a configured recovery path; an existing permission can always be turned
off. Automatic checks are labelled as AURA detections rather than user notes.
The backend used in the successful run was not changed.
