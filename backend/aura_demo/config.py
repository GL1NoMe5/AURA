from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


@dataclass(frozen=True)
class DemoSettings:
    root: Path
    data: Path
    origin: str
    access_code: str
    context: str = "default"
    image: str = "sissbruecker/linkding@sha256:5e37e0c0a3cb915ab02e432a0c4026e8ad8b3a0f27c9ca9b7cc4feff7d2eefce"
    port: int = 18097
    max_runs: int = 20
    max_calls: int = 100
    calls_per_run: int = 7
    session_runs: int = 2
    cooldown: int = 60
    deadline: int = 480

    def __post_init__(self):
        url = urlsplit(self.origin)
        if (url.path or url.query or url.fragment or url.username or url.password
                or not url.hostname or url.scheme not in ("http", "https")):
            raise ValueError("AURA_DEMO_ORIGIN must be one exact origin, without a trailing slash")
        if url.scheme != "https" and url.hostname not in ("127.0.0.1", "localhost"):
            raise ValueError("A public demo requires HTTPS")
        if not 24 <= len(self.access_code) <= 128:
            raise ValueError("Use a random demo access code of 24–128 characters")
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", self.context):
            raise ValueError("Invalid Docker context")
        if not re.fullmatch(r"(?:[a-z0-9./_-]+@)?sha256:[a-f0-9]{64}", self.image):
            raise ValueError("Pin the Linkding image to a digest or local image ID")
        for value, low, high in ((self.port, 1024, 65535), (self.max_runs, 1, 100),
                                (self.max_calls, 1, 700), (self.calls_per_run, 1, 7),
                                (self.session_runs, 1, 3), (self.cooldown, 0, 3600),
                                (self.deadline, 60, 600)):
            if type(value) is not int or not low <= value <= high:
                raise ValueError("Invalid demo bound")

    @classmethod
    def from_env(cls):
        root = Path(__file__).resolve().parents[2]
        return cls(root=root, data=Path(os.environ.get("AURA_DEMO_DATA", str(root / ".local/judging-demo"))).resolve(),
                   origin=os.environ["AURA_DEMO_ORIGIN"], access_code=os.environ["AURA_DEMO_ACCESS_CODE"],
                   context=os.environ.get("AURA_DEMO_DOCKER_CONTEXT", "default"),
                   max_runs=int(os.environ.get("AURA_DEMO_MAX_RUNS", "20")),
                   max_calls=int(os.environ.get("AURA_DEMO_MAX_CALLS", "100")))
