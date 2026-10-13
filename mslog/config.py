"""환경설정. 값은 환경변수로 덮어쓸 수 있다."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("MSLOG_DATA_DIR", BASE_DIR / "data"))

# AI 문서 인식: "claude"(Anthropic API) 또는 "none"(수동 입력/엑셀 업로드만 사용)
# 외부 AI 서비스로 이미지가 전송되므로 사내 보안정책 확인 후 사용한다.
OCR_PROVIDER = os.environ.get(
    "MSLOG_OCR_PROVIDER", "claude" if os.environ.get("ANTHROPIC_API_KEY") else "none"
)
OCR_MODEL = os.environ.get("MSLOG_OCR_MODEL", "claude-sonnet-5-5")

# 이 값 미만의 신뢰도로 인식된 행은 '인식 불확실'로 분류한다.
CONFIDENCE_THRESHOLD = float(os.environ.get("MSLOG_CONFIDENCE_THRESHOLD", "0.85"))

# 운행일지 vs 계근표 수량 허용 오차. 기본은 엄격(0). 예: 0.005 = 0.5%
QTY_TOLERANCE_RATIO = float(os.environ.get("MSLOG_QTY_TOLERANCE_RATIO", "0"))
# 총중량 - 공차중량 = 순중량 내부 정합성 허용 오차(비율)
WEIGHT_CHECK_TOLERANCE = 0.005

DOC_TYPES = ("운행일지", "계근표")

# ERP 입력용 엑셀 컬럼 (헤더, 내부 키). 실제 ERP 입력 양식을 받으면 이 목록만 수정한다.
ERP_COLUMNS = [
    ("납품일자", "date"),
    ("차량번호", "vehicle"),
    ("거래처코드", "customer_code"),
    ("거래처명", "customer_name"),
    ("사업장", "site"),
    ("품목", "item"),
    ("수량", "quantity"),
    ("단위", "unit"),
    ("운행일지 건수", "n_log"),
    ("계근표 건수", "n_weigh"),
]
