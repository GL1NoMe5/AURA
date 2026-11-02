# Live judging demonstration

The `/try` page runs a real, bounded configuration recovery on a disposable
Linkding instance. The judge supplies a demo access code, never a Baseten key.
The normal local AURA API is not exposed by this server.

**Open [the hosted demo](https://aura.34.70.253.200.sslip.io/try).**
Use the demo access code supplied with the submission. Visitors do not need a
Google Cloud account, Docker, or their own API key. The code is shared separately
with intended testers and is not included in this public repository.

## What the visitor runs

1. Create a fresh Linkding database and one test bookmark, with no user data.
2. Check the health endpoint and authenticated bookmarks API; capture working
   settings with the production `OpsService`.
3. Deliberately change the internal port from 9090 to 9091. The gateway keeps
   addressing 9090, so real requests fail.
4. Let the ordinary monitor detect the failure. `OpsInvestigator` calls Baseten
   `meta-models/muse-spark-open-1026` to investigate the collected evidence.
5. If the model proposes the pre-authorized configuration restore, the ordinary
   policy/action executor validates and applies it. Other actions are not
   authorized. No substitute diagnosis is used if the model fails.
6. Require production recovery verification and a final independent collection:
   working HTTP probes, the original image, port 9090, and exactly one dispatched
   policy-authorized restoration. Only then does the portrait reassemble.
7. Remove only resources bearing this demo's exact ownership label.

This is an intentionally introduced configuration fault in an existing
application. It demonstrates detection, diagnosis, policy enforcement and
verified restoration. It does not demonstrate autonomous code repair, a newly
discovered defect, or a memory optimization. Code repair remains a separate
human-approved AURA workflow.

The screen reports actual measurements and provider usage. Timing varies. An
unavailable provider, invalid answer, forbidden proposal, timeout or interrupted
run produces an incomplete result, never a fabricated recovery.

## Local start

Prerequisites: the existing project Python environment, built frontend, Docker
running Linux containers, and a server-side Baseten key. The pinned Linkding
image must already be available in the selected Docker context.

```powershell
docker --context desktop-linux pull sissbruecker/linkding@sha256:5e37e0c0a3cb915ab02e432a0c4026e8ad8b3a0f27c9ca9b7cc4feff7d2eefce
npm --prefix frontend run build
backend/.venv/Scripts/python.exe tools/demo/configure.py --origin http://127.0.0.1:8019 --context desktop-linux
backend/.venv/Scripts/python.exe tools/demo/serve.py
```

Open `http://127.0.0.1:8019/try`. The generated judge code is in
`.local/judging-demo/access-code.txt`. Private settings are in
`.local/judging-demo/server.env`; an existing project `.env` can supply the
Baseten key. Configuration refuses to overwrite an existing file. Never put
either private file in Git, the frontend, the submission's public source, or a
URL. Share only the judge access code with the intended testers.

## Linux host preparation

Use a **dedicated x86-64 Linux VM** with Docker Engine and Compose v2, Node 22+
and uv. Start with 2 vCPU and 4 GiB RAM; no GPU is needed on the server. The
browser renders the portrait. This is a deployment starting point, not a
measured capacity guarantee.

Install OS prerequisites on the eventual server using their official packages.
The repository setup script does not install or reconfigure system services.
Keep the checkout at `/opt/aura`, owned by a dedicated `aura` account. That
account must be able to use Docker. Docker access is host-level authority, so
this VM should contain no unrelated applications or private workloads.

As the `aura` user, from `/opt/aura`:

```bash
bash tools/demo/setup.sh
backend/.venv/bin/python tools/demo/configure.py --origin https://demo.example.com
backend/.venv/bin/python tools/demo/check_fixture.py --run
backend/.venv/bin/python tools/demo/serve.py
```

Replace the reserved example domain with the real hostname. Dependencies,
Python downloads and package caches stay under the checkout. Docker image and
volume storage is managed by the VM's Docker daemon. The fixture check creates
and removes only a new test instance; it makes no provider requests.

For unattended operation, install the supplied
`infra/demo/aura-demo.service` into systemd after checking
the account and paths. It runs one process on `127.0.0.1:8019` and allows time for
cleanup on shutdown. Do not run multiple workers or multiple clones against
the same data directory or fixture port.

Put Caddy in front using `infra/demo/Caddyfile`, replacing its
hostname with the same exact origin configured above. Validate the file before
reload. Public DNS must point to the VM, with ports 80 and 443 reachable. Keep
8019 and 18097 private; restrict SSH separately. The reverse proxy must preserve
the original `Host`. No permissive CORS or forwarded-host trust is needed.
See [Caddy's official HTTPS proxy guide](https://caddyserver.com/docs/quick-starts/reverse-proxy).

The backend validates the configured public host even for localhost requests.
After configuring a public domain, a local health check therefore uses:

```bash
curl --fail -H 'Host: demo.example.com' http://127.0.0.1:8019/health
```

The hosted instance runs on a dedicated Google Cloud e2-medium VM with 4 GiB
RAM, Ubuntu 24.04 and a 30 GB disk. Caddy serves a verified Let's Encrypt
certificate. Only HTTP/HTTPS are publicly reachable; SSH uses Google IAP.
The GitHub Pages site hosts the film and links to this separate Python/Docker
controller.

## Limits and stored data

Default limits are one active recovery, 2 runs per browser session, 20 total
runs, 100 logical model completions, at most 7 completions per run, and an
8-minute run deadline. Each completion has at most one transport retry, a
4096-token output limit and a 160,000-byte input limit. A new run requires room
for its entire possible 7-call allocation. Failed requests consume their
reservation. A 60-second cooldown separates completed runs.

The durable SQLite ledger counts all runs and requests across refreshes,
sign-outs and server restarts. The session limit is a convenience limit, not a
person's verified identity; deleting cookies does not bypass the shared cap.
These are request bounds, **not a dollar budget or confirmed billing amount**.
Inspect the provider's billing separately. VM costs are separate from Baseten.

The only visitor inputs are an access code, a new-run request and cancellation
of their own run. There is no endpoint for arbitrary targets, prompts, shell
commands, model names, deployment approval or provider proxying. Request bodies
are bounded to 1 KiB. Sessions use opaque HttpOnly/SameSite cookies, Secure under
HTTPS. Login attempts are rate limited. The server exposes sanitized reports,
not raw logs, environment variables, source trees or private SQLite files.

Per-run evidence lives under `.local/judging-demo/runs/`; the global ledger is
`.local/judging-demo/demo.sqlite`. Keep this directory between restarts. Do not
delete it to reset quotas: it also records ownership and interrupted runs. A
restart stops interrupted runs and cleans their labelled resources without
resuming paid work. Failed cleanup blocks new runs for operator inspection.
Changing the access code does not revoke already-issued 12-hour sessions.

## Checks before sharing the link

- Run the focused tests: `python -m pytest backend/tests/test_judging_demo.py -q`.
- Build the frontend and run the explicit fixture check shown above.
- On the actual Linux host, complete one real run through `/try`; inspect the
  final verification, model usage, and owned-resource cleanup.
- Refresh during a run: it must reconnect without creating another run.
- Check that `/api/v2/status`, `/docs`, `/.env` and arbitrary files return 404.
- Confirm HTTPS, a Secure session cookie, inaccessible internal ports and a
  successful external-browser run before adding the link to the public README.

The automated tests cover access boundaries, host/origin checks, idempotency,
cross-session isolation, persistent quotas, input/output bounds, cancellation
during preparation, cleanup ownership, blocked cleanup and interrupted startup.
They do not replace real Linux-host deployment acceptance or guarantee that
every model response will succeed.

## Local verification, 2 November 2026

One live run through the new browser page completed on the development Windows
host using Linux Docker containers and the real Baseten adapter:

| Measurement | Result |
| --- | --- |
| Fault introduction to independently verified recovery | 89.596 seconds |
| Model completions / transport attempts | 3 / 3 |
| Provider-reported tokens | 26,174 total (19,313 input; 6,861 output) |
| Production verification | 19 observations; 0 incomplete; 15.609 seconds stable |
| Final health / authenticated bookmarks requests | 200 / 200 |
| Cleanup | No owned containers, networks or volumes left |

The page was reloaded during verification and reconnected to the same run.
The result is one successful configuration recovery, not a success-rate study.
Private evidence: `.local/judging-demo/live-result.json`, with production
investigation evidence under its corresponding `runs` directory. No code-fix
claim is attached to this availability result.

## Hosted verification, 2 November 2026

One complete run was started in Chrome through the public HTTPS page on the
dedicated Google Cloud Linux host. It used the real Baseten adapter and the
same production investigation, policy, action and verification code.

| Measurement | Result |
| --- | --- |
| Fault introduction to independently verified recovery | 97.787 seconds |
| Model completions / transport attempts | 3 / 3 |
| Provider-reported tokens | 27,684 total (19,163 input; 8,521 output) |
| Production verification | 18 observations; 0 incomplete; 15.760 seconds stable |
| Final health / authenticated bookmarks requests | 200 / 200 |
| Cleanup | Complete; no owned containers, networks or volumes left |

Reloading the browser during the run reconnected to the same investigation.
The durable ledger recorded exactly one run and three model reservations.
The Linux fixture check and all 17 focused demo tests also passed on this host.
HTTPS validation, HTTP-to-HTTPS redirection, Secure/HttpOnly/SameSite cookies,
request-origin checks and the private API/file boundaries were checked externally.
Ports 22, 8019 and 18097 were unreachable from the external test PC; 80 and 443
were reachable. SSH remains available through authenticated Google IAP.

Private acceptance record: `.local/cloud/live-result.json`, run
`64ae2538b0164ce099255a6a22979922`. Application source commit:
`a638b859fe92d1a88e50d963bccd34af7e383a27`.
This confirms one hosted configuration recovery, not a success-rate guarantee
or autonomous code repair. The shared demo quota includes this acceptance run.
