"""docs/PRD.md 기능 요구사항(F-ID)별 수용 테스트."""
import io
import json
from types import SimpleNamespace

import pytest

from mslog import config, export, ingest, review, service, verify
from mslog.db import now


def open_f(conn, ftype):
    return conn.execute("SELECT * FROM findings WHERE status='open' AND type=?", (ftype,)).fetchall()


def png(tmp_path, name="scan.png"):
    from PIL import Image
    p = tmp_path / name
    Image.new("RGB", (60, 40), "white").save(p)
    return p.read_bytes()


class FakeAnthropic:
    """anthropic.Anthropic 대체: 요청을 기록하고 준비된 tool_use 응답을 돌려준다."""
    rows = []
    error = None
    calls = []

    def __init__(self, *a, **k):
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kw):
        FakeAnthropic.calls.append(kw)
        if FakeAnthropic.error:
            raise FakeAnthropic.error
        part = SimpleNamespace(type="tool_use", name="record_rows", input={"rows": FakeAnthropic.rows})
        return SimpleNamespace(content=[part])


@pytest.fixture
def fake_ai(monkeypatch):
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", FakeAnthropic)
    monkeypatch.setattr(config, "OCR_PROVIDER", "claude")
    FakeAnthropic.rows, FakeAnthropic.error, FakeAnthropic.calls = [], None, []
    return FakeAnthropic


# F1 -----------------------------------------------------------------
def test_F1_accepts_image_pdf_excel_and_rejects_others(conn, upload_dir, tmp_path):
    ok_img, _ = service.register_file(conn, upload_dir, "a.png", png(tmp_path), "계근표", "t")
    ok_pdf, _ = service.register_file(conn, upload_dir, "b.pdf", b"%PDF-1.4 x", "운행일지", "t")
    bad, msg = service.register_file(conn, upload_dir, "c.docx", b"x", "운행일지", "t")
    bad2, _ = service.register_file(conn, upload_dir, "d.png", b"y", "기타", "t")
    assert ok_img and ok_pdf and bad is None and "지원하지 않는" in msg and bad2 is None
    kinds = {r["file_name"]: r["source_kind"] for r in conn.execute("SELECT * FROM documents")}
    assert kinds == {"a.png": "image", "b.pdf": "pdf"}
    assert all((upload_dir / r["stored_name"]).exists() for r in conn.execute("SELECT * FROM documents"))


# F2 -----------------------------------------------------------------
def test_F2_ai_extraction_flags_uncertainty_and_never_guesses(conn, upload_dir, tmp_path, fake_ai):
    fake_ai.rows = [
        {"delivery_date": "2026-10-13", "vehicle_no": "경기12가3456", "customer": "한국가스", "item": "프로판",
         "quantity": 12000, "unit": "kg", "confidence": 0.97, "uncertain_fields": []},
        {"delivery_date": "10/13", "vehicle_no": "서울80바1234", "customer": "대한LPG", "item": "부탄",
         "quantity": 8000, "unit": "kg", "confidence": 0.6, "uncertain_fields": ["quantity", "customer"]},
    ]
    doc_id, msg = service.register_file(conn, upload_dir, "log.png", png(tmp_path), "운행일지", "t")
    assert "완료" in msg
    call = fake_ai.calls[0]
    assert call["tool_choice"] == {"type": "tool", "name": "record_rows"}
    assert call["messages"][0]["content"][0]["type"] == "image"

    a, b = conn.execute("SELECT * FROM records ORDER BY id").fetchall()
    assert a["customer_code"] == "C001"                       # 별칭 매칭
    assert b["delivery_date"] is None                         # 연도 없는 날짜는 추정하지 않음
    assert {"delivery_date", "quantity", "customer_raw"} <= set(json.loads(b["uncertain_fields"]))
    detail = " ".join(f["detail"] for f in open_f(conn, "인식불확실"))
    assert "신뢰도 60%" in detail and "누락 항목: 납품일자" in detail
    assert json.loads(b["ai_json"])["delivery_date"] == "10/13"   # AI 원본 보존


def test_F2_pdf_sent_as_document_block(conn, upload_dir, fake_ai):
    fake_ai.rows = []
    service.register_file(conn, upload_dir, "x.pdf", b"%PDF-1.4 hello", "계근표", "t")
    assert fake_ai.calls[0]["messages"][0]["content"][0]["type"] == "document"
    assert conn.execute("SELECT ocr_message FROM documents").fetchone()[0].startswith("인식된 행이 없습니다")


def test_F2_api_failure_isolated_to_document(conn, upload_dir, tmp_path, fake_ai):
    fake_ai.error = RuntimeError("boom")
    doc_id, msg = service.register_file(conn, upload_dir, "x.png", png(tmp_path), "계근표", "t")
    d = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    assert d["ocr_status"] == "인식실패" and "boom" in d["ocr_message"]


# F4 -----------------------------------------------------------------
def _pair(conn, log_qty, weigh_qty, doc_ids=(1, 2)):
    conn.execute("INSERT INTO documents(doc_type,file_name,stored_name,file_hash,source_kind,uploaded_at,ocr_status)"
                 " VALUES('운행일지','a','a','h1','excel',?,'완료'),('계근표','b','b','h2','excel',?,'완료')",
                 (now(), now()))
    base = {"delivery_date": "2026-10-20", "vehicle_no": "12가3456", "customer": "대한LPG", "item": "프로판",
            "unit": "kg"}
    ingest.add_record(conn, 1, "운행일지", {**base, "quantity": log_qty})
    ingest.add_record(conn, 2, "계근표", {**base, "quantity": weigh_qty})
    conn.commit()
    verify.run_verification(conn)


def test_F4_tolerance_is_configurable(conn, monkeypatch):
    _pair(conn, 12000, 11900)
    assert len(open_f(conn, "수량불일치")) == 1                  # 기본: 엄격
    monkeypatch.setattr(config, "QTY_TOLERANCE_RATIO", 0.01)
    verify.run_verification(conn)
    assert open_f(conn, "수량불일치") == []                      # 1% 허용 → 자동 해소
    assert conn.execute("SELECT status FROM findings WHERE type='수량불일치'").fetchone()[0] == "auto_closed"


def test_F4_incompatible_units_are_flagged_not_compared(conn):
    _pair(conn, 12000, 12000)
    conn.execute("UPDATE records SET unit='통' WHERE doc_type='운행일지'")
    conn.commit()
    verify.run_verification(conn)
    assert any("단위 불일치" in f["detail"] for f in open_f(conn, "매칭실패"))
    assert open_f(conn, "수량불일치") == []


# F5 -----------------------------------------------------------------
def test_F5_missing_hint_when_counterpart_has_different_customer(conn):
    _pair(conn, 12000, 12000)
    conn.execute("UPDATE records SET customer_raw='서해에너지', customer_code='C003', customer_name='서해에너지' "
                 "WHERE doc_type='계근표'")
    conn.commit()
    verify.run_verification(conn)
    miss = open_f(conn, "자료누락")
    assert len(miss) == 2 and all("거래처/품목이 다른" in m["detail"] for m in miss)


# F6 -----------------------------------------------------------------
def test_F6_exclude_needs_reason_and_removes_from_verification(loaded):
    rid = loaded.execute("SELECT id FROM records WHERE vehicle_no='서울80바1234'").fetchone()["id"]
    with pytest.raises(review.ReviewError):
        review.exclude_record(loaded, rid, "t", " ")
    review.exclude_record(loaded, rid, "t", "시험 운행")
    assert not any("서울" in (f["vehicle_no"] or "") for f in open_f(loaded, "자료누락"))
    review.restore_record(loaded, rid, "t")
    assert any("서울" in (f["vehicle_no"] or "") for f in open_f(loaded, "자료누락"))


def test_F6a_edit_after_approval_returns_to_unreviewed(loaded):
    rid = loaded.execute("SELECT id FROM records WHERE customer_code='C003' AND doc_type='운행일지'").fetchone()["id"]
    review.approve_record(loaded, rid, "t")
    review.save_record(loaded, rid, {"site": "당진 2센터"}, "t")
    r = loaded.execute("SELECT * FROM records WHERE id=?", (rid,)).fetchone()
    assert r["review_status"] == "미검수" and r["reviewed_by"] is None


def test_F6a_required_fields_block_approval(conn):
    _pair(conn, 12000, 12000)
    review.save_record(conn, 1, {"item": ""}, "t")
    with pytest.raises(review.ReviewError, match="품목"):
        review.approve_record(conn, 1, "t")


# F7 -----------------------------------------------------------------
def test_F7_resolved_stays_resolved_but_new_discrepancy_reopens(conn):
    _pair(conn, 12000, 11800)
    f = open_f(conn, "수량불일치")[0]
    verify.resolve_finding(conn, f["id"], "t", "원본 확인")
    review.save_record(conn, 2, {"quantity": "11700"}, "t")   # 값이 바뀌면 새로운 오류로 재판정
    fs = open_f(conn, "수량불일치")
    assert len(fs) == 1 and fs[0]["id"] != f["id"] and fs[0]["compare_qty"] == 11700


def test_F7_assign(loaded):
    f = open_f(loaded, "수량불일치")[0]
    verify.assign_finding(loaded, f["id"], "김담당")
    assert loaded.execute("SELECT assignee FROM findings WHERE id=?", (f["id"],)).fetchone()[0] == "김담당"


# F8 -----------------------------------------------------------------
def test_F8_period_filter_and_sheet_contents(loaded):
    for r in loaded.execute("SELECT id FROM records WHERE customer_code='C003'").fetchall():
        review.approve_record(loaded, r["id"], "t")
    assert len(export.verified_rows(loaded, "2026-10-13", "2026-10-13")) == 1
    assert export.verified_rows(loaded, "2026-10-14", "2026-10-31") == []
    # 승인되지 않은 정상 건·오류 건은 오류·미검증 목록에 포함
    kinds = {u[0] for u in export.unverified_rows(loaded)}
    assert kinds == {"오류", "미검수"}


# F10 ----------------------------------------------------------------
def test_F10_rematch_after_master_update(tmp_path, samples):
    from mslog.web import create_app
    app = create_app(tmp_path)
    c = app.test_client()
    upload = lambda f, k: c.post("/upload", data={"doc_type": k, "files": ((samples / f).open("rb"), f)},
                                 content_type="multipart/form-data")
    upload("운행일지_샘플.xlsx", "운행일지")
    page = c.get("/").get_data(as_text=True)
    assert "거래처 기준정보가 비어 있어" in page                  # 기준정보 없음 경고
    c.post("/masters/import", data={"kind": "customers", "file": ((samples / "customers.xlsx").open("rb"), "c.xlsx")},
           content_type="multipart/form-data")
    c.post("/masters/rematch")
    import sqlite3
    row = sqlite3.connect(app.config["DB_PATH"]).execute(
        "SELECT COUNT(*) FROM records WHERE customer_code IS NULL AND customer_raw!='코리아에너지'").fetchone()
    assert row[0] == 0
