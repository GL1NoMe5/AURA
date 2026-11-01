from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from aura_ops.patches import Candidate, _validate_candidate, _assert_candidate_redaction_stable
from aura_ops.models import OpsError

@pytest.mark.parametrize("reverse", [False, True])
def test_independent_edits_canonical_bytes(reverse):
    edits = [dict(path="app.py", old="x = 1", new="x = 3"), dict(path="app.py", old="y = 2", new="y = 4")]
    candidate = Candidate(summary="change", edits=edits[::-1] if reverse else edits,
                          regression_test="def test_ok(): assert True")
    result = _validate_candidate(candidate, {"app.py": b"x = 1\r\ny = 2\r\n"}, {"app.py"})
    assert result["app.py"] == b"x = 3\r\ny = 4\r\n"
    assert len(candidate.edits) == 1
    assert candidate.edits[0].old == "x = 1\r\ny = 2"

@pytest.mark.parametrize("old", ["x = 1", "1"])
def test_overlapping_edits_rejected(old):
    candidate = Candidate(summary="change", edits=[dict(path="app.py",old="x = 1",new="x = 3"),dict(path="app.py",old=old,new="2")],regression_test="def test_ok(): assert True")
    with pytest.raises(OpsError, match="overlap"):
        _validate_candidate(candidate,{"app.py": b"x = 1\n"},{"app.py"})

def test_fixture_identifiers_are_not_secrets():
    _assert_candidate_redaction_stable([], [], 'def test_a():\n    token = factory()\n    create_user(password="test")\n    assert True\n')

@pytest.mark.parametrize("text", ['# password = "hunter2"', 'value = "Bearer abcdefgh"', 'value = "actual-private-value"'])
def test_secret_rejection_has_no_value(text):
    with pytest.raises(OpsError) as exc:
        _assert_candidate_redaction_stable([], ["actual-private-value"], text)
    assert exc.value.code == "patch_candidate_redaction_conflict"
    assert text not in exc.value.message

@pytest.mark.parametrize("edits,source", [
    ([dict(path="app.py",old="x",new="z")], b"x=1; x=2"),
    ([dict(path="app.py",old="x",new="z"),dict(path="app.py",old="z",new="q")], b"x=1"),
    ([dict(path="app.py",old="1",new="(")], b"x=1"),
    ([dict(path="other.py",old="x",new="z")], b"x=1"),
])
def test_invalid_edits_fail(edits, source):
    candidate=Candidate(summary="change",edits=edits,regression_test="def test_ok(): assert True")
    with pytest.raises(OpsError):
        _validate_candidate(candidate,{"app.py":source},{"app.py"})

def test_adjacent_anchors():
    candidate=Candidate(summary="change",edits=[dict(path="app.py",old="x=",new="y="),dict(path="app.py",old="1",new="2")],regression_test="def test_ok(): assert True")
    assert _validate_candidate(candidate,{"app.py":b"x=1"},{"app.py"})["app.py"] == b"y=2"
