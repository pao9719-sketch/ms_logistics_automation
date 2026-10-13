"""거래처·품목 기준정보 관리 및 매칭.

자동 매칭은 코드/명칭/별칭의 정확 일치만 인정한다. 유사 명칭은 후보로만 제시하고
담당자가 확정한다(오매칭으로 인한 잘못된 정상 처리 방지).
"""
import difflib
import re

from .normalize import name_key


def aliases(text):
    return [a.strip() for a in re.split(r"[,;|]", text or "") if a.strip()]


def match_customer(conn, raw):
    """(code, name, 유사후보 문자열). 정확 일치가 없으면 code/name은 None."""
    if not raw or not str(raw).strip():
        return None, None, None
    key = name_key(raw)
    rows = conn.execute("SELECT * FROM customers").fetchall()
    for r in rows:
        keys = {name_key(r["name"]), name_key(r["code"])} | {name_key(a) for a in aliases(r["aliases"])}
        if key in keys:
            return r["code"], r["name"], None
    by_key = {name_key(r["name"]): r for r in rows}
    close = difflib.get_close_matches(key, list(by_key), n=1, cutoff=0.6)
    if close:
        r = by_key[close[0]]
        return None, None, f"{r['name']}({r['code']})"
    return None, None, None


def match_item(conn, raw):
    """기준정보 품목명(정식명)을 반환. 미일치면 None."""
    if not raw or not str(raw).strip():
        return None
    key = name_key(raw)
    for r in conn.execute("SELECT * FROM items").fetchall():
        keys = {name_key(r["name"])} | {name_key(a) for a in aliases(r["aliases"])}
        if key in keys:
            return r["name"]
    return None


def masters_loaded(conn):
    c = conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]
    i = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    return c > 0, i > 0


def _pick(row, names):
    for n in names:
        if n in row and row[n] not in (None, ""):
            return str(row[n]).strip()
    return None


def import_customers(conn, rows):
    n = 0
    for row in rows:
        code = _pick(row, ["거래처코드", "코드", "code"])
        name = _pick(row, ["거래처명", "거래처", "상호", "name"])
        if not code or not name:
            continue
        conn.execute(
            "INSERT INTO customers(code,name,aliases) VALUES(?,?,?) "
            "ON CONFLICT(code) DO UPDATE SET name=excluded.name, aliases=excluded.aliases",
            (code, name, _pick(row, ["별칭", "alias", "aliases"])),
        )
        n += 1
    conn.commit()
    return n


def import_items(conn, rows):
    n = 0
    for row in rows:
        name = _pick(row, ["품목명", "품목", "name"])
        if not name:
            continue
        conn.execute(
            "INSERT INTO items(name,unit,aliases) VALUES(?,?,?) "
            "ON CONFLICT(name) DO UPDATE SET unit=excluded.unit, aliases=excluded.aliases",
            (name, _pick(row, ["단위", "unit"]), _pick(row, ["별칭", "alias", "aliases"])),
        )
        n += 1
    conn.commit()
    return n
