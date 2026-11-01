import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from aura_worker.worker import WorkerApp
import aura_worker.worker as worker_module


def test_rejection_logs_actual_worker_values(monkeypatch):
    class Cache:
        def process_one_job(self): return False
        def snapshot(self):
            return {'retained_cache_bytes': 100663296, 'ceiling_bytes': 100663296,
                    'eviction_enabled': False, 'failed_jobs': 7}
    app = WorkerApp(Cache())
    lines = []
    def log(line):
        lines.append(line)
        app._stop.set()
    monkeypatch.setattr(worker_module, '_log', log)
    app._scheduler_loop()
    assert len(lines) == 1
    assert 'job rejected: cache ceiling reached' in lines[0]
    assert 'failed_jobs=7' in lines[0]
    assert 'eviction_enabled=False' in lines[0]
