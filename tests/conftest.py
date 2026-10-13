import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from mslog import config, db, excel_in, masters, service  # noqa: E402


@pytest.fixture(scope="session")
def samples(tmp_path_factory):
    out = tmp_path_factory.mktemp("samples")
    subprocess.run([sys.executable, str(ROOT / "scripts" / "make_samples.py"), str(out)], check=True,
                   capture_output=True)
    return out


@pytest.fixture
def conn(tmp_path, samples):
    path = tmp_path / "t.db"
    db.init_db(path)
    c = db.connect(path)
    masters.import_customers(c, excel_in.read_table(samples / "customers.xlsx"))
    masters.import_items(c, excel_in.read_table(samples / "items.xlsx"))
    yield c
    c.close()


@pytest.fixture
def upload_dir(tmp_path):
    return tmp_path / "uploads"


@pytest.fixture
def loaded(conn, samples, upload_dir):
    for name, kind in [("운행일지_샘플.xlsx", "운행일지"), ("계근표_샘플.xlsx", "계근표")]:
        doc_id, msg = service.register_file(conn, upload_dir, name, (samples / name).read_bytes(), kind, "tester")
        assert doc_id, msg
    return conn
