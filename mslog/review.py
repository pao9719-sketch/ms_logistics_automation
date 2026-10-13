"""담당자 검수: 수정(전후값 이력), 승인, 제외."""
import json

from . import masters, verify
from .db import now
from .normalize import clean_plate, normalize_date, parse_number

EDITABLE = ["delivery_date", "vehicle_no", "customer_raw", "site", "item", "quantity", "unit",
            "gross_weight", "tare_weight", "note"]
NUMERIC = {"quantity", "gross_weight", "tare_weight"}
FIELD_LABELS = {
    "delivery_date": "납품일자", "vehicle_no": "차량번호", "customer_raw": "거래처", "customer_code": "거래처코드",
    "site": "사업장", "item": "품목", "quantity": "수량", "unit": "단위",
    "gross_weight": "총중량", "tare_weight": "공차중량", "note": "비고",
}


class ReviewError(Exception):
    pass


def _coerce(field, value):
    v = (value or "").strip()
    if field == "delivery_date":
        if not v:
            return None
        d = normalize_date(v)
        if not d:
            raise ReviewError(f"납품일자 형식을 확인하세요: '{v}' (예: 2026-10-13)")
        return d
    if field in NUMERIC:
        if not v:
            return None
        n = parse_number(v)
        if n is None:
            raise ReviewError(f"{FIELD_LABELS[field]}은(는) 숫자여야 합니다: '{v}'")
        return n
    if field == "vehicle_no":
        return clean_plate(v)
    return v or None


def _same(a, b):
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-9
        except (TypeError, ValueError):
            return False
    return (a or None) == (b or None)


def get_record(conn, record_id):
    r = conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
    if r is None:
        raise ReviewError("기록을 찾을 수 없습니다.")
    return r


def save_record(conn, record_id, form, user):
    """수정값 저장. 변경이 있으면 이력을 남기고 미검수로 되돌린다. 변경 필드 수 반환."""
    rec = get_record(conn, record_id)
    new, changes = {}, []
    for f in EDITABLE:
        if f in form:
            nv = _coerce(f, form[f])
            if not _same(nv, rec[f]):
                new[f] = nv
    code_in = (form.get("customer_code") or "").strip() or None
    if "customer_code" in form and code_in != rec["customer_code"]:
        if code_in:
            row = conn.execute("SELECT * FROM customers WHERE code=?", (code_in,)).fetchone()
            if row is None:
                raise ReviewError(f"기준정보에 없는 거래처코드입니다: {code_in}")
            new["customer_code"], new["customer_name"] = row["code"], row["name"]
        else:
            new["customer_code"], new["customer_name"] = None, None
    elif "customer_raw" in new:
        code, name, _ = masters.match_customer(conn, new["customer_raw"])
        new["customer_code"], new["customer_name"] = code, name
    if "item" in new and new["item"]:
        new["item"] = masters.match_item(conn, new["item"]) or new["item"]
    if not new:
        return 0

    unc = set(json.loads(rec["uncertain_fields"] or "[]"))
    for f in list(new):
        unc.discard(f)
    ts = now()
    for f, nv in new.items():
        if f == "customer_name":
            continue
        changes.append((record_id, f, None if rec[f] is None else str(rec[f]),
                        None if nv is None else str(nv), user, ts))
    sets = ", ".join(f"{f}=?" for f in new)
    conn.execute(
        f"UPDATE records SET {sets}, uncertain_fields=?, review_status='미검수', reviewed_by=NULL, reviewed_at=? "
        f"WHERE id=?", [*new.values(), json.dumps(sorted(unc), ensure_ascii=False), None, record_id])
    conn.executemany("INSERT INTO edits(record_id,field,old_value,new_value,editor,edited_at) VALUES(?,?,?,?,?,?)",
                     changes)
    conn.commit()
    verify.run_verification(conn)
    return len(changes)


def approve_record(conn, record_id, user):
    """검수 완료. 필수값 누락 또는 열린 오류(인식불확실 제외)가 있으면 승인할 수 없다."""
    rec = get_record(conn, record_id)
    missing = [lbl for f, lbl in verify.REQUIRED if rec[f] in (None, "")]
    if not (rec["customer_raw"] or rec["customer_code"]):
        missing.append("거래처")
    if missing:
        raise ReviewError("필수 항목이 비어 있습니다: " + ", ".join(missing))
    blocking = conn.execute(
        """SELECT f.type, f.detail FROM finding_records fr JOIN findings f ON f.id=fr.finding_id
           WHERE fr.record_id=? AND f.status='open' AND f.type!='인식불확실'""", (record_id,)).fetchall()
    if blocking:
        raise ReviewError("미처리 오류가 있어 승인할 수 없습니다: "
                          + "; ".join(f"[{b['type']}] {b['detail']}" for b in blocking))
    conn.execute("UPDATE records SET review_status='검수완료', reviewed_by=?, reviewed_at=? WHERE id=?",
                 (user, now(), record_id))
    conn.commit()
    verify.run_verification(conn)


def exclude_record(conn, record_id, user, reason):
    reason = (reason or "").strip()
    if not reason:
        raise ReviewError("제외 사유를 입력하세요.")
    rec = get_record(conn, record_id)
    conn.execute("UPDATE records SET review_status='제외', reviewed_by=?, reviewed_at=? WHERE id=?",
                 (user, now(), record_id))
    conn.execute("INSERT INTO edits(record_id,field,old_value,new_value,editor,edited_at) VALUES(?,?,?,?,?,?)",
                 (record_id, "review_status", rec["review_status"], f"제외: {reason}", user, now()))
    conn.commit()
    verify.run_verification(conn)


def restore_record(conn, record_id, user):
    get_record(conn, record_id)
    conn.execute("UPDATE records SET review_status='미검수', reviewed_by=NULL, reviewed_at=NULL WHERE id=?",
                 (record_id,))
    conn.execute("INSERT INTO edits(record_id,field,old_value,new_value,editor,edited_at) VALUES(?,?,?,?,?,?)",
                 (record_id, "review_status", "제외", "미검수(복원)", user, now()))
    conn.commit()
    verify.run_verification(conn)
