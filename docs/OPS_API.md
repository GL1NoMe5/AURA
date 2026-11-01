# Operations API v2

Local FastAPI controller; production launcher binds to `127.0.0.1`. Existing applications are external resources. No read, disconnect, cancellation or AURA shutdown stops a registered container. Browser mutations reject non-local origins. This version is a single-user local tool, not an authenticated remote service.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/v2/status` | Authoritative snapshot, event watermark, targets and active investigation |
| GET/POST | `/api/v2/targets` | List or enroll up to three observe-only targets |
| GET | `/api/v2/targets/{id}` | Target description and latest observation |
| POST | `/api/v2/targets/{id}/policy` | Explicit automatic-investigation/action permissions |
| POST | `/api/v2/targets/{id}/snapshot` | Capture a fresh healthy, version-bound baseline |
| POST | `/api/v2/targets/{id}/disconnect` | Cancel AURA work and stop observation, retaining application |
| POST | `/api/v2/targets/{id}/reconnect` | Resume observation; no paid investigation by reconnect alone |
| GET/POST | `/api/v2/investigations` | List or queue a manual investigation |
| GET | `/api/v2/investigations/{id}` | Diagnosis, evidence, usage, patch and verification |
| POST | `/api/v2/investigations/{id}/cancel` | Cancel pending work and invalidate pending proposals |
| GET | `/api/v2/proposals` | Durable proposals and their execution status |
| POST | `/api/v2/proposals/{id}/decision` | `{proposal_hash, approve}`; exact current proposal only |
| POST | `/api/v2/patches` | `{investigation_id}`; prepare at most two isolated candidates |
| POST | `/api/v2/deployments` | `{investigation_id}`; build reviewed image/proposal, no application mutation |
| GET | `/api/v2/artifacts/{id}` | Integrity-checked artifact download by opaque ID |
| GET | `/api/v2/events?after=N` | Bounded sequence-ordered event history |
| WS | `/ws/ops` | Initial snapshot, then durable sequence-ordered events |

Models and validation live in `backend/aura_ops/models.py`. IDs are not filesystem paths. A target binds an explicit Docker context and container, HTTP probes, optional exact source/image provenance, a trusted isolated test profile, Compose configuration and independent recovery operations. Credentials are referenced by environment variable name, never stored in target JSON. Import the laboratory's generated JSON rather than manually guessing immutable image IDs.

An operator-enrolled `patch_profile.scratch_dirs` may list up to four unique plain top-level names for temporary application data. Each receives a 64 MiB, no-exec tmpfs at `/workspace/<name>` inside the existing container memory limit. A scratch mount cannot conceal source or trusted test files. Linkding requires `["data"]` for its Huey SQLite initialization. Models cannot change this profile.

Code deployment and source-bound configuration/image replay also require an operator-enrolled `deployment_launch` with an explicit direct server executable and literal arguments. The reviewed Compose overlay replaces bootstrap startup for both deployment and rollback. It must not invoke database migrations. The linkding fixture exports a direct uWSGI launch; verify it on actual Docker before acceptance.

## State and evidence

Investigations move from queued to investigating, optionally awaiting approval, executing and verifying. `mitigated` means availability only. `fix_proposed` means isolated tests passed, without deployment. `verified_fixed` requires an approved candidate and independent runtime operation/integrity/memory verification. `needs_human` and `cancelled` are distinct outcomes.

Each event has `seq`, `timestamp`, `type`, `target_id`, `investigation_id` and `payload`. The sequence persists across restarts. Slow subscribers may see a gap; reload the authoritative status snapshot instead of inferring missing transitions. Reconnecting never creates an investigation.

Actions re-read durable policy, enrollment, approval and target identity after awaited preflight work and immediately before dispatch. An unknown execution outcome remains unknown and is not blindly retried. On AURA restart, incomplete execution is marked unknown and proposals expire. Observed model usage does not imply verified billing cost.

## Credential files

The controller loads only explicitly named probe credentials from `.local/ops/credentials.env`, `.local/ops-lab/credentials.env`, `.local/ops-healthchecks/credentials.env`, and the ignored project `.env`. Restart AURA after seeding or rotating laboratory credentials. Baseten configuration remains separate from probe authorization. No container environment is passed wholesale to the model.
