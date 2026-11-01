"""Real mmap-backed cache simulator for the gaming-session scenario (SIM-001).

Fixed production sizes (task contract):
- block = 2 MiB, healthy = 8 blocks (16 MiB), ceiling = 96 MiB payload.
- The ceiling applies to cache payload bytes, never to total process RSS.
"""
from __future__ import annotations

import mmap
import threading
import time
from typing import Callable, Optional

BLOCK_SIZE_BYTES = 2 * 1024 * 1024
HEALTHY_BLOCK_COUNT = 8
HEALTHY_CACHE_BYTES = BLOCK_SIZE_BYTES * HEALTHY_BLOCK_COUNT
CEILING_BYTES = 96 * 1024 * 1024
CEILING_BLOCK_COUNT = CEILING_BYTES // BLOCK_SIZE_BYTES
JOBS_PER_SECOND = 4.0
JOB_INTERVAL_SECONDS = 1.0 / JOBS_PER_SECOND
PAGE_SIZE_BYTES = 4096


def _allocate_touched_block(block_size: int) -> mmap.mmap:
    """Allocate one anonymous mapping and physically touch every page."""
    block = mmap.mmap(-1, block_size)
    for offset in range(0, block_size, PAGE_SIZE_BYTES):
        block[offset] = 0x41
    block[block_size - 1] = 0x42
    return block


class CacheSimulator:
    """Thread-safe retained-cache model driven by synthetic jobs.

    Healthy mode: each completed job allocates a fresh block and evicts the
    oldest one, so exactly ``healthy_blocks`` blocks are retained.
    Fault mode (eviction disabled): new blocks are retained until the data
    ceiling; at the ceiling jobs are rejected and counted as failures.
    """

    def __init__(
        self,
        block_size: int = BLOCK_SIZE_BYTES,
        healthy_blocks: int = HEALTHY_BLOCK_COUNT,
        ceiling_bytes: int = CEILING_BYTES,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if block_size <= 0 or healthy_blocks <= 0 or ceiling_bytes < block_size:
            raise ValueError("invalid cache geometry")
        self._block_size = block_size
        self._healthy_blocks = healthy_blocks
        self._ceiling_bytes = ceiling_bytes
        self._clock = clock
        self._lock = threading.Lock()
        self._blocks: list[mmap.mmap] = []
        self._initialized = False
        self._eviction_enabled = True
        self._completed_jobs = 0
        self._failed_jobs = 0
        self._initialized_at: Optional[float] = None

    # -- lifecycle ------------------------------------------------------
    def initialize(self) -> dict:
        with self._lock:
            if self._initialized:
                raise RuntimeError("already_initialized")
            for _ in range(self._healthy_blocks):
                self._blocks.append(_allocate_touched_block(self._block_size))
            self._initialized = True
            self._eviction_enabled = True
            self._initialized_at = self._clock()
            return self._snapshot_locked()

    def close(self) -> None:
        with self._lock:
            self._close_all_locked()
            self._initialized = False

    def _close_all_locked(self) -> None:
        for block in self._blocks:
            try:
                block.close()
            except (BufferError, ValueError):
                pass
        self._blocks = []

    # -- fault / recovery ----------------------------------------------
    def inject_cache_retention(self) -> dict:
        with self._lock:
            self._require_initialized()
            self._eviction_enabled = False
            return self._snapshot_locked()

    def restore_cache_policy(self) -> dict:
        with self._lock:
            self._require_initialized()
            self._eviction_enabled = True
            while len(self._blocks) > self._healthy_blocks:
                self._blocks.pop(0).close()
            return self._snapshot_locked()

    # -- jobs ------------------------------------------------------------
    def process_one_job(self) -> bool:
        """Run one synthetic job. Returns False when rejected at ceiling."""
        with self._lock:
            self._require_initialized()
            if self.retained_bytes_locked() + self._block_size > self._ceiling_bytes:
                self._failed_jobs += 1
                return False
            self._blocks.append(_allocate_touched_block(self._block_size))
            if self._eviction_enabled:
                while len(self._blocks) > self._healthy_blocks:
                    self._blocks.pop(0).close()
            self._completed_jobs += 1
            return True

    # -- introspection ---------------------------------------------------
    def _require_initialized(self) -> None:
        if not self._initialized:
            raise RuntimeError("not_initialized")

    def retained_bytes_locked(self) -> int:
        return len(self._blocks) * self._block_size

    def snapshot(self) -> dict:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> dict:
        return {
            "initialized": self._initialized,
            "eviction_enabled": self._eviction_enabled,
            "block_count": len(self._blocks),
            "block_size_bytes": self._block_size,
            "retained_cache_bytes": self.retained_bytes_locked(),
            "healthy_cache_bytes": self._healthy_blocks * self._block_size,
            "ceiling_bytes": self._ceiling_bytes,
            "completed_jobs": self._completed_jobs,
            "failed_jobs": self._failed_jobs,
        }
