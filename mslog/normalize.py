"""날짜·차량번호·거래처명·수량·단위 표준화."""
import datetime as dt
import re

_PLATE_CORE = re.compile(r"(\d{2,3}[가-힣]\d{4})")

WEIGHT_UNITS = {
    "kg": 1.0, "㎏": 1.0, "킬로": 1.0, "킬로그램": 1.0,
    "톤": 1000.0, "ton": 1000.0, "t": 1000.0, "mt": 1000.0,
    "g": 0.001,
}


def clean_plate(v):
    if v is None:
        return None
    s = re.sub(r"[\s\-_.]", "", str(v))
    return s or None


def plate_key(v):
    """지역명 유무와 무관하게 같은 차량이면 같은 키가 되도록 한다."""
    s = clean_plate(v)
    if not s:
        return None
    m = _PLATE_CORE.search(s)
    return m.group(1) if m else s


def normalize_date(v):
    """다양한 날짜 표기를 YYYY-MM-DD로. 해석 불가(연도 없음 등)면 None."""
    if v is None or v == "":
        return None
    if isinstance(v, dt.datetime):
        return v.date().isoformat()
    if isinstance(v, dt.date):
        return v.isoformat()
    s = str(v).strip()
    m = re.search(r"(\d{4})\D{0,2}(\d{1,2})\D{0,2}(\d{1,2})", s)
    if not m:
        m = re.match(r"^(\d{2})\D(\d{1,2})\D(\d{1,2})", s)
        if not m:
            return None
        y = 2000 + int(m.group(1))
    else:
        y = int(m.group(1))
    try:
        return dt.date(y, int(m.group(2)), int(m.group(3))).isoformat()
    except ValueError:
        return None


def parse_number(v):
    if v is None or v == "":
        return None
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    m = re.search(r"-?\d+(?:\.\d+)?", str(v).replace(",", ""))
    return float(m.group()) if m else None


def split_quantity(v):
    """'12,500kg' -> (12500.0, 'kg')."""
    num = parse_number(v)
    unit = None
    if isinstance(v, str):
        m = re.search(r"[A-Za-z㎏톤]+", v)
        unit = m.group() if m else None
    return num, unit


def name_key(v):
    if v is None:
        return ""
    s = str(v).lower()
    for token in ("주식회사", "(주)", "㈜", "(유)", "유한회사"):
        s = s.replace(token, "")
    return re.sub(r"[\s\-_.()·,]", "", s)


def to_base(qty, unit):
    """(값, 차원). 무게 단위는 kg로 환산. 단위 미기재는 차원 '?'(어느 쪽과도 비교 가능)."""
    if qty is None:
        return None, "?"
    u = (unit or "").strip().lower()
    if not u:
        return qty, "?"
    if u in WEIGHT_UNITS:
        return qty * WEIGHT_UNITS[u], "kg"
    return qty, u
