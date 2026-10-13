import datetime as dt

from mslog.normalize import clean_plate, name_key, normalize_date, parse_number, plate_key, split_quantity, to_base


def test_dates():
    assert normalize_date("2026-10-13") == "2026-10-13"
    assert normalize_date("2026.10.13") == "2026-10-13"
    assert normalize_date("2026년 10월 3일") == "2026-10-03"
    assert normalize_date("20261013") == "2026-10-13"
    assert normalize_date("26/10/13") == "2026-10-13"
    assert normalize_date(dt.datetime(2026, 10, 13, 9, 0)) == "2026-10-13"
    assert normalize_date("10/13") is None          # 연도 없음 → 추정하지 않음
    assert normalize_date("2026-13-40") is None


def test_plate():
    assert clean_plate("경기 12가-3456") == "경기12가3456"
    assert plate_key("경기 12가 3456") == plate_key("12가3456") == "12가3456"
    assert plate_key("서울80바1234") == "80바1234"


def test_numbers_and_units():
    assert parse_number("12,500") == 12500
    assert split_quantity("12,500kg") == (12500, "kg")
    assert to_base(12, "톤") == (12000, "kg")
    assert to_base(5, "통") == (5, "통")
    assert to_base(5, None) == (5, "?")


def test_name_key():
    assert name_key("(주) 한국가스 산업") == name_key("한국가스산업")
