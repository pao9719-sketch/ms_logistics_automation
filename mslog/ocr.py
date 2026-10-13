"""AI 문서 인식(운행일지·계근표 이미지/PDF → 구조화 행).

Claude 비전 모델을 tool use로 호출해 JSON 스키마에 맞는 결과를 받는다.
확인할 수 없는 값은 추정하지 않고 null + uncertain_fields로 돌려주도록 지시한다.
"""
import base64
import io
from pathlib import Path

from . import config


class OcrUnavailable(Exception):
    pass


_ROW_PROPS = {
    "delivery_date": {"type": ["string", "null"], "description": "납품/계근 일자. YYYY-MM-DD, 연도가 없으면 null"},
    "vehicle_no": {"type": ["string", "null"], "description": "차량번호"},
    "customer": {"type": ["string", "null"], "description": "거래처명(문서에 적힌 그대로)"},
    "site": {"type": ["string", "null"], "description": "사업장/납품장소"},
    "item": {"type": ["string", "null"], "description": "품목명"},
    "quantity": {"type": ["number", "null"], "description": "납품수량 또는 계근표 순중량"},
    "unit": {"type": ["string", "null"], "description": "quantity의 단위(kg, 톤 등)"},
    "gross_weight": {"type": ["number", "null"], "description": "총중량(계근표)"},
    "tare_weight": {"type": ["number", "null"], "description": "공차중량(계근표)"},
    "confidence": {"type": "number", "description": "이 행 전체 인식 신뢰도 0~1"},
    "uncertain_fields": {
        "type": "array",
        "items": {"type": "string", "enum": [
            "delivery_date", "vehicle_no", "customer", "site", "item",
            "quantity", "unit", "gross_weight", "tare_weight"]},
        "description": "판독이 불확실하거나 흐릿한 필드",
    },
}

TOOL = {
    "name": "record_rows",
    "description": "문서에서 읽은 납품 기록 행을 등록한다.",
    "input_schema": {
        "type": "object",
        "properties": {
            "rows": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": _ROW_PROPS,
                    "required": ["confidence", "uncertain_fields"],
                },
            }
        },
        "required": ["rows"],
    },
}

PROMPT = (
    "첨부된 {doc_type}에서 납품 기록을 한 행씩 추출해 record_rows 도구로 제출하세요.\n"
    "- 문서에 실제로 적힌 값만 옮기고, 읽을 수 없거나 없는 항목은 null로 두세요. 추정·보정 금지.\n"
    "- 흐릿하거나 두 가지로 읽히는 값은 uncertain_fields에 넣고 confidence를 낮추세요.\n"
    "- 수량은 문서의 숫자 그대로, 단위는 문서에 적힌 단위를 그대로 쓰세요(환산 금지).\n"
    "- 계근표라면 순중량을 quantity에, 총중량/공차중량을 gross_weight/tare_weight에 넣으세요.\n"
    "- 한 문서에 여러 건이 있으면 건마다 별도 행으로 제출하세요."
)

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _prepare_image(path):
    from PIL import Image

    img = Image.open(path)
    img = img.convert("RGB")
    if max(img.size) > 2400:
        img.thumbnail((2400, 2400))
    quality = 88
    while True:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=quality)
        if buf.tell() <= 4_500_000 or quality <= 50:
            break
        quality -= 12
    return "image/jpeg", base64.standard_b64encode(buf.getvalue()).decode()


def extract(path, doc_type):
    """문서 파일에서 행 목록(dict)을 추출한다. OCR 미설정이면 OcrUnavailable."""
    if config.OCR_PROVIDER != "claude":
        raise OcrUnavailable("AI 인식이 설정되지 않았습니다(ANTHROPIC_API_KEY). 수동 입력 또는 엑셀 업로드를 사용하세요.")
    import anthropic

    path = Path(path)
    if path.suffix.lower() == ".pdf":
        block = {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf",
                       "data": base64.standard_b64encode(path.read_bytes()).decode()},
        }
    else:
        media, data = _prepare_image(path)
        block = {"type": "image", "source": {"type": "base64", "media_type": media, "data": data}}

    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=config.OCR_MODEL,
        max_tokens=4096,
        tools=[TOOL],
        tool_choice={"type": "tool", "name": TOOL["name"]},
        messages=[{"role": "user", "content": [block, {"type": "text", "text": PROMPT.format(doc_type=doc_type)}]}],
    )
    for part in resp.content:
        if part.type == "tool_use" and part.name == TOOL["name"]:
            return list(part.input.get("rows", []))
    raise RuntimeError("AI 응답에서 인식 결과를 찾지 못했습니다.")
