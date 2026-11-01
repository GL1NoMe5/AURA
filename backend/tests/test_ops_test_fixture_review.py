"""Keep safe filename fixtures without permitting direct source inspections."""
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from aura_ops.models import OpsError
from aura_ops.patches import Candidate, _validate_candidate


def validate(snippet):
    candidate = Candidate(summary="Behavior fix", edits=[{
        "path": "app.py", "old": "return 0", "new": "return 42",
    }], regression_test=snippet)
    return _validate_candidate(candidate, {"app.py": b"def answer():\n    return 0\n"}, {"app.py"})


@pytest.mark.parametrize("snippet", [
    "from app import answer\ndef test_a(tmp_path):\n"
    "    sample = tmp_path / 'app.py'\n    sample.write_text('fixture')\n"
    "    assert sample.read_text() == 'fixture'\n    assert answer() == 42\n",
    "from app import answer\ndef test_a(tmp_path):\n"
    "    sample = tmp_path / 'nested' / 'app.py'\n    assert answer() == 42\n",
    "from app import answer\ndef test_a(tmp_path):\n"
    "    sample = tmp_path.joinpath('app.py')\n    assert answer() == 42\n",
    "from django.core.files.uploadedfile import SimpleUploadedFile\n"
    "from app import answer\ndef test_a():\n"
    "    sample = SimpleUploadedFile('app.py', b'fixture')\n    assert answer() == 42\n",
    "from django.core.files.uploadedfile import SimpleUploadedFile as Upload\n"
    "from app import answer\ndef test_a():\n"
    "    sample = Upload(name='app.py', content=b'fixture')\n    assert answer() == 42\n",
    "import django.core.files.uploadedfile as uploads\n"
    "from app import answer\ndef test_a():\n"
    "    sample = uploads.SimpleUploadedFile('app.py', b'fixture')\n    assert answer() == 42\n",
])
def test_same_name_temporary_and_upload_fixtures_allowed(snippet):
    assert validate(snippet)["app.py"] == b"def answer():\n    return 42\n"


@pytest.mark.parametrize("snippet", [
    "def test_a():\n    assert 'return 42' in open('app.py').read()\n",
    "from builtins import open as read\ndef test_a():\n"
    "    assert 'return 42' in read('app.py').read()\n",
    "import io as files\ndef test_a():\n    assert 'return 42' in files.open('app.py').read()\n",
    "from pathlib import Path\ndef test_a():\n    assert 'return 42' in Path('app.py').read_text()\n",
    "from pathlib import Path as P\ndef test_a():\n    assert b'return 42' in P('app.py').read_bytes()\n",
    "import pathlib as paths\ndef test_a():\n    assert 'return 42' in paths.Path('app.py').read_text()\n",
    "from pathlib import Path\ndef test_a():\n    name = 'app.py'\n"
    "    source = Path(name)\n    assert 'return 42' in source.read_text()\n",
    "def test_a(tmp_path):\n    assert 'return 42' in (tmp_path / '/workspace/app.py').read_text()\n",
    "def test_a(tmp_path):\n    assert 'return 42' in (tmp_path / '../app.py').read_text()\n",
    "from pathlib import Path\ndef test_a(tmp_path):\n    tmp_path = Path('.')\n"
    "    assert 'return 42' in (tmp_path / 'app.py').read_text()\n",
    "from pathlib import Path\ndef test_a():\n    tmp_path = Path('.')\n"
    "    assert 'return 42' in tmp_path.joinpath('app.py').read_text()\n",
    "from app import SimpleUploadedFile\ndef test_a():\n"
    "    assert SimpleUploadedFile('app.py')\n",
])
def test_direct_source_inspection_still_rejected(snippet):
    with pytest.raises(OpsError) as error:
        validate(snippet)
    assert error.value.code == "patch_test_invalid"
