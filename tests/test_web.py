import io
import re

import pytest

from mslog.web import create_app


@pytest.fixture
def client(tmp_path):
    app = create_app(tmp_path)
    app.config["TESTING"] = True
    return app.test_client()


def upload(client, path, doc_type):
    return client.post("/upload", data={"doc_type": doc_type, "files": (io.BytesIO(path.read_bytes()), path.name)},
                       content_type="multipart/form-data", follow_redirects=True)


def test_end_to_end_flow(client, samples):
    for kind, f in [("customers", "customers.xlsx"), ("items", "items.xlsx")]:
        r = client.post("/masters/import", data={"kind": kind, "file": ((samples / f).open("rb"), f)},
                        content_type="multipart/form-data", follow_redirects=True)
        assert r.status_code == 200
    r = upload(client, samples / "운행일지_샘플.xlsx", "운행일지")
    assert "검수" in r.get_data(as_text=True) and r.status_code == 200
    upload(client, samples / "계근표_샘플.xlsx", "계근표")

    html = client.get("/").get_data(as_text=True)
    assert "운행일지_샘플.xlsx" in html and "수량불일치" in html
    f = client.get("/findings?type=수량불일치").get_data(as_text=True)
    assert "12,000" in f and "11,800" in f

    # 기록 승인 시도: 오류가 있으면 거부, 메시지 노출
    doc = client.get("/documents/2").get_data(as_text=True)
    rid = int(re.search(r'/records/(\d+)/approve', doc).group(1))
    r = client.post(f"/records/{rid}/approve", follow_redirects=True)
    assert r.status_code == 200

    x = client.get("/export.xlsx")
    assert x.status_code == 200 and x.data[:2] == b"PK"
    assert client.get("/reports").status_code == 200
    assert client.get("/masters").status_code == 200


def test_image_upload_without_ocr_waits_for_manual_entry(client, tmp_path):
    from PIL import Image
    p = tmp_path / "scan.png"
    Image.new("RGB", (40, 40), "white").save(p)
    r = upload(client, p, "계근표")
    html = r.get_data(as_text=True)
    assert "인식대기" in html and "행 추가" in html
    r = client.post("/documents/1/add", headers={"Referer": "/documents/1"}, follow_redirects=True)
    assert "기록 #1" in r.get_data(as_text=True)
