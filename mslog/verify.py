"""운행일지 ↔ 계근표 자동 대조 및 오류 탐지.

오류 유형(우선순위 순): 매칭실패 > 수량불일치 > 자료누락 > 중복의심 > 인식불확실
원칙: 규칙을 통과하지 못한 건은 담당자가 처리하기 전까지 열린(open) 상태로 남긴다.
"""
import hashlib
import json
from collections import defaultdict

from . import config, masters
from .db import now
from .normalize import name_key, plate_key, to_base

PRIORITY = ["매칭실패", "수량불일치", "자료누락", "중복의심", "인식불확실"]
LOG, WEIGH = "운행일지", "계근표"
REQUIRED = [("delivery_date", "납품일자"), ("vehicle_no", "차량번호"), ("item", "품목"), ("quantity", "수량")]


def customer_key(r):
    if r["customer_code"]:
        return r["customer_code"]
    return "raw:" + name_key(r["customer_raw"]) if r["customer_raw"] else ""


def group_key(r):
    return (r["delivery_date"], plate_key(r["vehicle_no"]), customer_key(r), name_key(r["item"]))


def groupable(r):
    d, v, c, i = group_key(r)
    return bool(d and v and c and i and r["quantity"] is not None)


def build_groups(records):
    """{(일자,차량키,거래처키,품목키): {운행일지:[...], 계근표:[...]}}"""
    groups = defaultdict(lambda: {LOG: [], WEIGH: []})
    for r in records:
        if groupable(r):
            groups[group_key(r)][r["doc_type"]].append(r)
    return groups


def sum_quantity(recs):
    """그룹 수량 합계와 단위. 단위가 섞여 있으면 무게는 kg로 환산."""
    units = {(r["unit"] or "").strip() for r in recs}
    if len(units) <= 1:
        return sum(r["quantity"] for r in recs), (units.pop() if units else "") or ""
    return sum(to_base(r["quantity"], r["unit"])[0] for r in recs), "kg"


def _within_tolerance(a, b):
    tol = config.QTY_TOLERANCE_RATIO * max(abs(a), abs(b))
    return abs(a - b) <= tol + 1e-9


def _label(r):
    return r["customer_name"] or r["customer_raw"] or "-"


def run_verification(conn):
    recs = [dict(r) for r in conn.execute("SELECT * FROM records WHERE review_status!='제외'")]
    by_id = {r["id"]: r for r in recs}
    has_cust, has_item = masters.masters_loaded(conn)
    item_names = {r["name"] for r in conn.execute("SELECT name FROM items")}
    computed = {}

    def add(ftype, ids, detail, sig="", **meta):
        ids = sorted(ids)
        h = hashlib.sha1(json.dumps([ftype, ids, sig], ensure_ascii=False).encode()).hexdigest()[:20]
        computed[f"{ftype}:{h}"] = {"type": ftype, "ids": ids, "detail": detail, **meta}

    # 1) 레코드 단위: 인식 불확실 / 기준정보 매칭 실패
    for r in recs:
        meta = dict(delivery_date=r["delivery_date"], vehicle_no=r["vehicle_no"],
                    customer=_label(r), item=r["item"], unit=r["unit"])
        if r["review_status"] == "미검수":
            reasons = []
            missing = [lbl for f, lbl in REQUIRED if r[f] in (None, "")]
            if not (r["customer_raw"] or r["customer_code"]):
                missing.append("거래처")
            if missing:
                reasons.append("누락 항목: " + ", ".join(missing))
            unc = json.loads(r["uncertain_fields"] or "[]")
            if unc:
                reasons.append("판독 불확실 필드: " + ", ".join(unc))
            if r["confidence"] is not None and r["confidence"] < config.CONFIDENCE_THRESHOLD:
                reasons.append(f"인식 신뢰도 {r['confidence']:.0%}")
            g, t, q = r["gross_weight"], r["tare_weight"], r["quantity"]
            if None not in (g, t, q) and abs((g - t) - q) > config.WEIGHT_CHECK_TOLERANCE * max(abs(q), 1):
                reasons.append(f"총중량-공차중량({g - t:g})과 순중량({q:g}) 불일치")
            if reasons:
                add("인식불확실", [r["id"]], " / ".join(reasons), **meta)

        if has_cust and r["customer_raw"] and not r["customer_code"]:
            _, _, sugg = masters.match_customer(conn, r["customer_raw"])
            add("매칭실패", [r["id"]],
                f"거래처 '{r['customer_raw']}'이(가) 기준정보에 없음" + (f" (유사 후보: {sugg})" if sugg else ""),
                sig="cust", **meta)
        if has_item and r["item"] and r["item"] not in item_names:
            add("매칭실패", [r["id"]], f"품목 '{r['item']}'이(가) 기준정보에 없음", sig="item", **meta)

    # 2) 중복 의심: 같은 종류 문서에서 일자·차량·거래처·품목·수량이 완전히 같은 기록
    dup = defaultdict(list)
    for r in recs:
        if groupable(r):
            dup[(r["doc_type"],) + group_key(r) + (round(r["quantity"], 6),)].append(r)
    for key, rs in dup.items():
        if len(rs) > 1:
            docs = sorted({r["document_id"] for r in rs})
            where = f"서로 다른 문서({', '.join('#%d' % d for d in docs)})" if len(docs) > 1 else f"같은 문서(#{docs[0]})"
            r0 = rs[0]
            add("중복의심", [r["id"] for r in rs],
                f"{key[0]} 동일 기록 {len(rs)}건이 {where}에 있음. 복수 운행이면 '정상'으로 처리하세요.",
                delivery_date=r0["delivery_date"], vehicle_no=r0["vehicle_no"], customer=_label(r0),
                item=r0["item"], base_qty=r0["quantity"], unit=r0["unit"])

    # 3) 운행일지 ↔ 계근표 대조: 누락 / 수량 불일치 / 단위 불일치
    groups = build_groups(recs)
    unmatched_by_dv = defaultdict(lambda: {LOG: [], WEIGH: []})
    pending = []
    for key, g in groups.items():
        logs, weighs = g[LOG], g[WEIGH]
        ids = [r["id"] for r in logs + weighs]
        first = (logs or weighs)[0]
        meta = dict(delivery_date=key[0], vehicle_no=first["vehicle_no"], customer=_label(first), item=first["item"])
        if logs and weighs:
            lq, lu = sum_quantity(logs)
            wq, wu = sum_quantity(weighs)
            (lv, ld), (wv, wd) = to_base(lq, lu), to_base(wq, wu)
            if "?" not in (ld, wd) and ld != wd:
                add("매칭실패", ids, f"단위 불일치: 운행일지 {lq:g}{lu} / 계근표 {wq:g}{wu}",
                    sig="unit", unit=f"{lu}/{wu}", **meta)
            elif not _within_tolerance(lv, wv):
                add("수량불일치", ids,
                    f"운행일지 {lq:g}{lu} ({len(logs)}건) / 계근표 {wq:g}{wu} ({len(weighs)}건)",
                    sig=f"{lv:.6f}|{wv:.6f}", base_qty=lq, compare_qty=wq, diff=lq - wq, unit=lu or wu, **meta)
        else:
            missing = WEIGH if logs else LOG
            have = LOG if logs else WEIGH
            pending.append((key, ids, meta, have, missing))
            unmatched_by_dv[(key[0], key[1])][have].append(first)

    for key, ids, meta, have, missing in pending:
        other = unmatched_by_dv[(key[0], key[1])][missing]
        hint = ""
        if other:
            alt = ", ".join(sorted({f"{_label(o)}/{o['item']}" for o in other}))
            hint = f" ※ 같은 일자·차량에 거래처/품목이 다른 {missing} 있음: {alt}"
        add("자료누락", ids, f"{have}만 있고 {missing}가 없음.{hint}", sig=have,
            base_qty=by_id[ids[0]]["quantity"], unit=by_id[ids[0]]["unit"], **meta)

    _sync(conn, computed)
    conn.commit()
    return {t: sum(1 for c in computed.values() if c["type"] == t) for t in PRIORITY}


def _sync(conn, computed):
    existing = {r["fkey"]: r for r in conn.execute("SELECT * FROM findings")}
    for fkey, c in computed.items():
        ex = existing.get(fkey)
        cols = (c.get("delivery_date"), c.get("vehicle_no"), c.get("customer"), c.get("item"),
                c.get("base_qty"), c.get("compare_qty"), c.get("diff"), c.get("unit"), c["detail"])
        if ex is None:
            cur = conn.execute(
                """INSERT INTO findings(fkey,type,delivery_date,vehicle_no,customer,item,base_qty,
                       compare_qty,diff,unit,detail,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (fkey, c["type"]) + cols + (now(),))
            fid = cur.lastrowid
        else:
            fid = ex["id"]
            conn.execute(
                """UPDATE findings SET delivery_date=?,vehicle_no=?,customer=?,item=?,base_qty=?,
                       compare_qty=?,diff=?,unit=?,detail=? WHERE id=?""", cols + (fid,))
            if ex["status"] == "auto_closed":   # 다시 발생 → 재오픈
                conn.execute("UPDATE findings SET status='open' WHERE id=?", (fid,))
        conn.execute("DELETE FROM finding_records WHERE finding_id=?", (fid,))
        conn.executemany("INSERT INTO finding_records VALUES(?,?)", [(fid, i) for i in c["ids"]])
    for fkey, ex in existing.items():
        if fkey not in computed and ex["status"] == "open":
            conn.execute("UPDATE findings SET status='auto_closed' WHERE id=?", (ex["id"],))


def open_status_by_record(conn):
    """{record_id: 가장 우선순위 높은 열린 오류 유형}"""
    rows = conn.execute(
        """SELECT fr.record_id, f.type FROM finding_records fr
           JOIN findings f ON f.id=fr.finding_id WHERE f.status='open'""").fetchall()
    best = {}
    for r in rows:
        cur = best.get(r["record_id"])
        if cur is None or PRIORITY.index(r["type"]) < PRIORITY.index(cur):
            best[r["record_id"]] = r["type"]
    return best


def resolve_finding(conn, finding_id, user, resolution):
    resolution = (resolution or "").strip()
    if not resolution:
        raise ValueError("조치 결과(사유)를 입력해야 처리할 수 있습니다.")
    conn.execute(
        "UPDATE findings SET status='resolved', resolution=?, resolved_by=?, resolved_at=? "
        "WHERE id=? AND status='open'", (resolution, user, now(), finding_id))
    conn.commit()


def assign_finding(conn, finding_id, assignee):
    conn.execute("UPDATE findings SET assignee=? WHERE id=?", ((assignee or "").strip() or None, finding_id))
    conn.commit()
