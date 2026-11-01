# Verification evidence semantics (OPS-VERIFY-GUARD-001)

What recovery verification requires, what a verified result proves, and what
it does not. For Codex UI reflection and operator interpretation.

## Required telemetry

Both availability (`OpsService.verify`) and functional (`verify_functional`)
verification require complete, fresh evidence before any verdict:

- Transport: no `error`, empty `collection_errors`. Any failed source
  (`collection_errors.<name>`) fails the observation as incomplete.
- Identity: full `container_id`/`image_id`/`started_at`, pinned across the run.
- Freshness: functional samples within −5..+30 s; availability samples 0..10 s old.
- Resources: `memory_bytes` and `memory_limit_bytes` must be finite,
  nonnegative `int`/`float` (bool is not a metric). CPU may be missing alone.
- Logs (functional only): must be a string. Empty `""` from a successful
  collection is valid; a missing/non-string value is incomplete.
- Runtime counters (functional only): integer `restart_count` required.
- Probes: every enrolled probe present with `ok: true` and its expected status.

Missing/failed essentials never verify. Availability resets its healthy
interval on incomplete samples and reports `telemetry_incomplete`; functional
fails closed with `verification_collection_failed` or
`verification_observation_incomplete` plus `failed_source`.

## Memory-limit semantics

There are two separate fields. `config.memory_limit_bytes` comes from
`HostConfig.Memory`; zero means no configured container limit. In contrast,
`resources.memory_limit_bytes` normally preserves Docker stats' reported limit,
which can be a positive host limit for an uncapped container. A positive
HostConfig limit is only a fallback when the stats limit is missing. This task
does not change the collector or claim an uncapped container always reports zero.

The validator supports an explicitly reported resource limit of zero as unlimited.
`None`/missing remains **unknown** and rejects the sample. Availability keeps the
`< 90%` usage gate against positive reported limits. An explicit zero skips that
ratio only; functional cgroup OOM deltas and worker-failure checks still apply.

## Container health semantics

A sample counts toward stable recovery only when `runtime.running` is `true`,
`runtime.status` is `"running"`, and `runtime.health` is `"healthy"` or
`"none"` (`"none"` = no Docker healthcheck configured, the collector default).
Explicit `"starting"`/`"unhealthy"`, paused/restarting/dead statuses, stopped
runtimes, missing required state, or inconsistent probe results never count.
Only fields `DockerTarget.collect()` actually emits are required.

## Coverage honesty

- `sampled_peak_memory_bytes` is the maximum of recorded samples with valid
  memory (`memory_samples` of `sample_count`), never an absolute peak.
- `in_transfer_samples` / `transfer_coverage`: samples taken while downloads
  ran. A transfer finishing between polls reports `no_in_transfer_sample`
  instead of inventing a data point or slowing the transfer. A verified result
  therefore proves the complete operation plus actual OOM/health checks under
  this workload — not a continuous memory profile. Sampling blind spots remain.
- A check failing before download launch has zero in-transfer samples, even if
  its initial resource sample was recorded. Missing cgroup evidence identifies
  `cgroup.memory_events` as the failed source.
- Availability reports `sample_count`, `incomplete_samples`, and stable
  `reason_code` (`recovery_observed`, `recovery_not_observed`,
  `telemetry_incomplete`, `identity_changed`); functional reports stable
  `reason` codes plus `failed_source` naming the failed required source.

## Worker-failure baselining

`baseline_worker_failures` preserves historical failure lines present before
the enrolled workload; only lines added during the monitored interval appear in
`new_worker_failures` and fail the run. New lines are never silently absorbed
into the baseline. Failures from container startup before functional monitoring
began are outside this interval (Argon 4.3 is explicitly out of scope here).

## UI fields to reflect

Availability: `reason_code`, `sample_count`, `incomplete_samples` (new).
Functional: `failed_source`, `in_transfer_samples`, `transfer_coverage`,
`memory_samples`, `baseline_worker_failures` (new). All other report fields
keep their shape; `reason` strings keep prior wording except the explicit
incomplete-telemetry suffix on availability timeout.
