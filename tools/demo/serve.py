"""Start only the restricted judging app; configuration comes from local secrets."""
import argparse
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "backend"))

if __name__ == "__main__":
    from dotenv import load_dotenv
    import uvicorn
    from aura_demo.app import create_demo_app
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8019)
    parser.add_argument("--env-file", type=Path, default=ROOT / ".local/judging-demo/server.env")
    args = parser.parse_args()
    # No provider access or external listener is created until the operator starts this command.
    load_dotenv(args.env_file, override=False)
    uvicorn.run(create_demo_app(), host="127.0.0.1", port=args.port,
                log_level="warning", access_log=False, proxy_headers=False,
                limit_concurrency=40, timeout_keep_alive=5)
