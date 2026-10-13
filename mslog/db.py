import datetime as dt
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_type TEXT NOT NULL,
    file_name TEXT NOT NULL,
    stored_name TEXT NOT NULL,
    file_hash TEXT NOT NULL UNIQUE,
    source_kind TEXT NOT NULL,          -- image | pdf | excel
    uploaded_at TEXT NOT NULL,
    uploaded_by TEXT,
    ocr_status TEXT NOT NULL,           -- 완료 | 인식대기 | 인식실패
    ocr_message TEXT
);
CREATE TABLE IF NOT EXISTS records (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL REFERENCES documents(id),
    doc_type TEXT NOT NULL,
    delivery_date TEXT,
    vehicle_no TEXT,
    customer_raw TEXT,
    customer_code TEXT,
    customer_name TEXT,
    site TEXT,
    item TEXT,
    quantity REAL,
    unit TEXT,
    gross_weight REAL,
    tare_weight REAL,
    confidence REAL,
    uncertain_fields TEXT NOT NULL DEFAULT '[]',
    ai_json TEXT,                       -- 인식 원본 값(수정해도 보존)
    note TEXT,
    review_status TEXT NOT NULL DEFAULT '미검수',   -- 미검수 | 검수완료 | 제외
    reviewed_by TEXT,
    reviewed_at TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_records_doc ON records(document_id);
CREATE INDEX IF NOT EXISTS ix_records_date ON records(delivery_date);
CREATE TABLE IF NOT EXISTS edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    record_id INTEGER NOT NULL REFERENCES records(id),
    field TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    editor TEXT,
    edited_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS findings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fkey TEXT NOT NULL UNIQUE,
    type TEXT NOT NULL,
    delivery_date TEXT,
    vehicle_no TEXT,
    customer TEXT,
    item TEXT,
    base_qty REAL,
    compare_qty REAL,
    diff REAL,
    unit TEXT,
    detail TEXT,
    status TEXT NOT NULL DEFAULT 'open',   -- open | resolved | auto_closed
    assignee TEXT,
    resolution TEXT,
    resolved_by TEXT,
    resolved_at TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS finding_records (
    finding_id INTEGER NOT NULL REFERENCES findings(id) ON DELETE CASCADE,
    record_id INTEGER NOT NULL REFERENCES records(id),
    PRIMARY KEY (finding_id, record_id)
);
CREATE TABLE IF NOT EXISTS customers (
    code TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    aliases TEXT
);
CREATE TABLE IF NOT EXISTS items (
    name TEXT PRIMARY KEY,
    unit TEXT,
    aliases TEXT
);
"""


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def connect(path):
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(path):
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = connect(path)
    conn.executescript(SCHEMA)
    conn.commit()
    conn.close()
