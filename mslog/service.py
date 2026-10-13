"""업로드 처리: 저장 → 중복 확인 → 인식/파싱 → 기록 생성 → 검증."""
import hashlib
import uuid
from pathlib import Path

from . import excel_in, ingest, ocr, verify
from .config import DOC_TYPES
from .db import now

EXCEL_EXT = {".xlsx", ".xlsm"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def source_kind(filename):
    ext = Path(filename).suffix.lower()
    if ext in EXCEL_EXT:
        return "excel"
    if ext == ".pdf":
        return "pdf"
    if ext in IMAGE_EXT:
        return "image"
    return None


def process_document(conn, upload_dir, doc_id):
    """문서의 기록을 (재)생성한다. AI 인식 미설정이면 '인식대기'로 남긴다."""
    doc = conn.execute("SELECT * FROM documents WHERE id=?", (doc_id,)).fetchone()
    path = Path(upload_dir) / doc["stored_name"]
    status, message, rows = "완료", None, []
    try:
        if doc["source_kind"] == "excel":
            rows = excel_in.read_rows(path)
        else:
            rows = ocr.extract(path, doc["doc_type"])
            if not rows:
                message = "인식된 행이 없습니다. 원본을 확인하고 필요하면 행을 직접 추가하세요."
    except ocr.OcrUnavailable as e:
        status, message = "인식대기", str(e)
    except Exception as e:  # 외부 API/파일 오류는 문서 단위로 격리
        status, message = "인식실패", f"{type(e).__name__}: {e}"
    for row in rows:
        ingest.add_record(conn, doc_id, doc["doc_type"], row)
    conn.execute("UPDATE documents SET ocr_status=?, ocr_message=? WHERE id=?", (status, message, doc_id))
    conn.commit()
    verify.run_verification(conn)
    return status, message, len(rows)


def register_file(conn, upload_dir, filename, data, doc_type, user):
    """(doc_id|None, 메시지). 동일 파일(내용 해시)이 이미 등록되어 있으면 등록하지 않는다."""
    if doc_type not in DOC_TYPES:
        return None, f"{filename}: 문서 구분이 올바르지 않습니다."
    kind = source_kind(filename)
    if kind is None:
        return None, f"{filename}: 지원하지 않는 형식입니다(이미지, PDF, xlsx)."
    digest = hashlib.sha256(data).hexdigest()
    dup = conn.execute("SELECT id, file_name FROM documents WHERE file_hash=?", (digest,)).fetchone()
    if dup:
        return None, f"{filename}: 이미 등록된 파일입니다(문서 #{dup['id']} {dup['file_name']}). 중복 등록하지 않았습니다."
    Path(upload_dir).mkdir(parents=True, exist_ok=True)
    stored = f"{uuid.uuid4().hex}{Path(filename).suffix.lower()}"
    (Path(upload_dir) / stored).write_bytes(data)
    cur = conn.execute(
        """INSERT INTO documents(doc_type,file_name,stored_name,file_hash,source_kind,uploaded_at,uploaded_by,ocr_status)
           VALUES(?,?,?,?,?,?,?,'인식대기')""",
        (doc_type, filename, stored, digest, kind, now(), user))
    conn.commit()
    doc_id = cur.lastrowid
    status, message, n = process_document(conn, upload_dir, doc_id)
    msg = f"{filename}: {status}"
    if status == "완료":
        msg += f" ({n}건 인식)"
    elif message:
        msg += f" - {message}"
    return doc_id, msg
