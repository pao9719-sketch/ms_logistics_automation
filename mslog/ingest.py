"""추출된 행을 표준화해 records로 저장한다."""
import json

from . import masters
from .db import now
from .normalize import clean_plate, normalize_date, parse_number, split_quantity

_TEXT_TO_FIELD = {"customer": "customer_raw"}


def _clean_text(v):
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def add_record(conn, document_id, doc_type, row, default_confidence=1.0):
    """행 1건 저장. 표준화 실패 필드는 uncertain_fields에 추가한다(값을 추정하지 않음)."""
    uncertain = {_TEXT_TO_FIELD.get(f, f) for f in (row.get("uncertain_fields") or [])}

    raw_date = row.get("delivery_date")
    date = normalize_date(raw_date)
    if raw_date not in (None, "") and date is None:
        uncertain.add("delivery_date")

    qty, qty_unit = split_quantity(row.get("quantity")) if isinstance(row.get("quantity"), str) \
        else (parse_number(row.get("quantity")), None)
    if row.get("quantity") not in (None, "") and qty is None:
        uncertain.add("quantity")
    unit = _clean_text(row.get("unit")) or qty_unit

    customer_raw = _clean_text(row.get("customer") if "customer" in row else row.get("customer_raw"))
    code, cname, _ = masters.match_customer(conn, customer_raw)
    item_raw = _clean_text(row.get("item"))
    item = masters.match_item(conn, item_raw) or item_raw

    conf = row.get("confidence")
    conf = default_confidence if conf is None else float(conf)

    cur = conn.execute(
        """INSERT INTO records(document_id, doc_type, delivery_date, vehicle_no, customer_raw,
               customer_code, customer_name, site, item, quantity, unit, gross_weight, tare_weight,
               confidence, uncertain_fields, ai_json, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            document_id, doc_type, date, clean_plate(row.get("vehicle_no")), customer_raw,
            code, cname, _clean_text(row.get("site")), item, qty, unit,
            parse_number(row.get("gross_weight")), parse_number(row.get("tare_weight")),
            conf, json.dumps(sorted(uncertain), ensure_ascii=False),
            json.dumps(row, ensure_ascii=False, default=str), now(),
        ),
    )
    return cur.lastrowid
