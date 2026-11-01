"""Project-local AURA runtime helper (RELEASE-001).

Modes:
  serve  - run the real API + built frontend on 127.0.0.1:8000 in this
           process (one uvicorn worker, no reload). Ctrl+C shuts down
           cleanly through the app lifespan; enrolled Docker applications stay running.
  setup  - explicit setup only: uv sync --locked for the backend, and
           (with --build-frontend) npm ci + production build. Nothing is
           installed on a normal serve launch.
  check  - validate prerequisites and port without starting anything.

BASETEN_API_KEY is never passed to frontend/build subprocesses or logs.
"""
from __future__ import annotations

import argparse
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

HOST = "127.0.0.1"
DEFAULT_PORT = 8000


class RuntimeSetupError(RuntimeError):
    pass


def venv_python(project_root: Path) -> Path:
    return project_root / "backend" / ".venv" / "Scripts" / "python.exe" \
        if os.name == "nt" else project_root / "backend" / ".venv" / "bin" / "python"


def frontend_dist(project_root: Path) -> Path:
    return project_root / "frontend" / "dist"


def check_prerequisites(project_root: Path) -> list[str]:
    """Return actionable English errors; empty list means ready to serve."""
    errors = []
    if not venv_python(project_root).is_file():
        errors.append(
            "Backend environment is missing (backend/.venv). "
            "Run Start-AURA.cmd --setup first.")
    if not (frontend_dist(project_root) / "index.html").is_file():
        errors.append(
            "Frontend build is missing (frontend/dist/index.html). "
            "Run Start-AURA.cmd --setup to build it.")
    return errors


def assert_port_free(port: int, host: str = HOST) -> None:
    """Fail clearly on a conflicting port; never kill any listener."""
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind((host, port))
    except OSError:
        raise RuntimeSetupError(
            f"Port {port} on {host} is already in use. Stop the program "
            "using that port yourself, then start AURA again. AURA never "
            "terminates unknown listeners.") from None
    finally:
        probe.close()


def build_subprocess_env() -> dict:
    """Environment for frontend/build tools: no provider key, ever."""
    env = dict(os.environ)
    env.pop("BASETEN_API_KEY", None)
    return env


def _run(command: list[str], cwd: Path, env: dict | None = None) -> None:
    print(f"> {' '.join(command)}")
    result = subprocess.run(command, cwd=str(cwd), env=env, check=False)
    if result.returncode != 0:
        raise RuntimeSetupError(
            f"Command failed with exit code {result.returncode}: "
            f"{command[0]}. See the output above.")


def run_setup(project_root: Path, *, build_frontend: bool = True,
              run_uv_sync: bool = True) -> None:
    if run_uv_sync:
        uv = shutil.which("uv")
        if uv is None:
            raise RuntimeSetupError(
                "uv was not found on PATH. Install uv, then run "
                "Start-AURA.cmd --setup again.")
        _run([uv, "sync", "--locked", "--project",
              str(project_root / "backend")], cwd=project_root,
             env=build_subprocess_env())
    if build_frontend:
        npm = shutil.which("npm")
        if npm is None:
            raise RuntimeSetupError(
                "Node.js/npm was not found on PATH. Install Node.js, "
                "then run Start-AURA.cmd --setup again.")
        frontend = project_root / "frontend"
        env = build_subprocess_env()
        _run([npm, "ci"], cwd=frontend, env=env)
        _run([npm, "run", "build"], cwd=frontend, env=env)


def serve(project_root: Path, port: int = DEFAULT_PORT) -> int:
    errors = check_prerequisites(project_root)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    try:
        assert_port_free(port)
    except RuntimeSetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3
    backend = project_root / "backend"
    if str(backend) not in sys.path:
        sys.path.insert(0, str(backend))
    os.chdir(backend)
    import uvicorn

    print(f"AURA is starting at http://{HOST}:{port}/")
    print("Press Ctrl+C to stop AURA. Enrolled applications remain running.")
    try:
        uvicorn.run(
            "aura_api.app:app", host=HOST, port=port, workers=1,
            reload=False)
    except KeyboardInterrupt:
        pass
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", default="serve",
                        choices=("serve", "setup", "check"))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--project-root", type=Path, default=None)
    parser.add_argument("--build-frontend", action="store_true",
                        help="setup mode: also run npm ci and the build")
    parser.add_argument("--skip-uv-sync", action="store_true",
                        help="setup mode: skip uv sync --locked")
    args = parser.parse_args(argv)
    root = args.project_root.resolve() if args.project_root \
        else Path(__file__).resolve().parents[2]
    try:
        if args.mode == "check":
            errors = check_prerequisites(root)
            if errors:
                for error in errors:
                    print(f"ERROR: {error}", file=sys.stderr)
                return 2
            assert_port_free(args.port)
            print(f"Prerequisites OK; http://{HOST}:{args.port}/ is free.")
            return 0
        if args.mode == "setup":
            run_setup(root, build_frontend=args.build_frontend,
                      run_uv_sync=not args.skip_uv_sync)
            print("Setup complete. Run Start-AURA.cmd to start AURA.")
            return 0
        return serve(root, port=args.port)
    except RuntimeSetupError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
