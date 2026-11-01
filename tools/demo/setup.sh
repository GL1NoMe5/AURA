#!/usr/bin/env bash
# Run as the demo user in a clean Linux checkout. No global package installs.
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.."
for tool in uv node npm docker; do
  command -v "$tool" >/dev/null || { echo "Missing prerequisite: $tool" >&2; exit 1; }
done
node -e 'if (+process.versions.node.split(".")[0] < 22) { console.error("Node 22 or newer required"); process.exit(1) }'
[[ "$(docker --context default info --format '{{.OSType}}')" == linux ]] || { echo 'Linux Docker Engine required' >&2; exit 1; }
docker compose version >/dev/null
export UV_CACHE_DIR="$PWD/.local/cache/uv"
export UV_PYTHON_INSTALL_DIR="$PWD/.local/python"
export npm_config_cache="$PWD/.local/cache/npm"
uv sync --locked --project backend --python 3.12
npm --prefix frontend ci
npm --prefix frontend run build
docker --context default pull sissbruecker/linkding@sha256:5e37e0c0a3cb915ab02e432a0c4026e8ad8b3a0f27c9ca9b7cc4feff7d2eefce
echo 'Prepared. Configure the private server.env with tools/demo/configure.py next.'
