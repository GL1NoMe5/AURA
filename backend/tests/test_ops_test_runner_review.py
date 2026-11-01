"""Actual generated-test layout, with a separately supplied runner (offline)."""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_ops_test_protocol import _run_real_runner, _validate_real
from aura_ops.patches import _REGRESSION
from aura_ops.test_protocol import check_after, check_before


def test_nested_generated_regression_imports_workspace_application(tmp_path):
    regression = "from app import answer\ndef test_answer():\n    assert answer() == 42\n"
    before = {"app.py": "def answer():\n    return 0\n", _REGRESSION: regression}
    code, output, bindings = _run_real_runner(
        tmp_path / "before", before, stage="before", expected_paths=[_REGRESSION])
    _, before_nodes, before_phases = _validate_real(output, code, bindings)
    assert check_before(before_nodes, before_phases, exit_code=code)

    after = {"app.py": "def answer():\n    return 42\n", _REGRESSION: regression}
    code, output, bindings = _run_real_runner(
        tmp_path / "after", after, stage="after", expected_paths=[_REGRESSION])
    _, nodes, phases = _validate_real(output, code, bindings)
    assert check_after(nodes, phases, exit_code=code, before_nodes=before_nodes)
