# AURA clean start

Start here with a fresh copy of the AURA source package on Windows.
No database, keys, virtual environment, or `node_modules` are shipped;
setup recreates them from the committed lockfiles.

## Prerequisites

- Windows 10 or 11 with PowerShell.
- `uv` on `PATH` (backend environment, locked by `backend/uv.lock`).
- Node.js with `npm` on `PATH` (frontend install and build, locked by
  `frontend/package-lock.json`).
- Python 3.10+ reachable by `uv` (see below if `uv` must download one).
- Network access to the Python and npm registries for the first install.
- Docker Desktop with Linux containers is required only for connected
  Docker targets. The empty-state first run below does not need Docker.

## Steps

1. Extract the ZIP into an empty folder, for example
   `C:\AURA-clean`. Keep the top-level layout (`backend/`, `frontend/`,
   `tools/`, `Start-AURA.cmd`, `.env.example`).
2. From that folder, run the one-time setup:
   `Start-AURA.cmd --setup`
   This runs `uv sync --locked --project backend`, then `npm ci` and the
   production frontend build. Nothing is installed outside the folder
   except the `uv`/Python and npm caches (override with `UV_CACHE_DIR`
   and `npm_config_cache` to keep caches inside the folder too).
3. Optional: save the Baseten server key locally with masked input:
   `Connect-Baseten.cmd`
   Without a key the app still starts; investigations stay unavailable
   (`provider_ready=false`) and no model request can be dispatched.
4. Start the app: `Start-AURA.cmd`
   Then open <http://127.0.0.1:8000/>. Startup never terminates another
   program using the requested port; a conflict fails with an explicit
   error instead.
5. Verify the empty state:
   - `GET /health` returns 200.
   - `GET /api/v2/status` returns 200 with `targets: []`,
     `active_id: null`, and `provider_ready: false` (without a key).
   - `/` serves the built UI, including `portrait/*.bin` and the audio file.
6. Stop with Ctrl+C. Enrolled applications, if any, keep running.

## Connect an application

Use **My apps > Connect an app** for observation and diagnosis. Open **?** for
the guided tour. A restart requires permission. For settings recovery or code
repair, follow [connection and repair setup](TARGET_MANAGEMENT.md#setting-up-an-app-for-repairs).
The app's maintainer supplies the source, existing test image and reviewed
deployment profile; the quick form does not build those automatically.

## If setup fails

- `uv was not found on PATH` — install `uv`, open a new terminal, retry.
- `Node.js/npm was not found on PATH` — install Node.js, retry.
- `Backend environment is missing` or `Frontend build is missing` —
  rerun `Start-AURA.cmd --setup` and read the first failing command.
- `Port 8000 ... is already in use` — stop the program using that port
  yourself; AURA never terminates unknown listeners.
- If `uv` reports no usable Python, install Python 3.10+ or let `uv`
  download one (set `UV_PYTHON_INSTALL_DIR` inside the folder first to
  keep that download local too).
