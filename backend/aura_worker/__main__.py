"""Entry point: python -m aura_worker (isolated cache worker)."""
from .worker import main

if __name__ == "__main__":
    raise SystemExit(main())
