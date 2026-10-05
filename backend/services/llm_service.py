import base64
import json
import logging
import io
from PIL import Image
from openai import AsyncOpenAI
from backend.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

# Mirrors backend.models.issue.IssueSeverity and the categories the prompt lists.
# Anything outside these sets is dropped in _normalise before it reaches the DB.
VALID_SEVERITIES = {"low", "medium", "high"}
VALID_ISSUE_TYPES = {
    "missing_tag",
    "dimension_mismatch",
    "unlabeled_element",
    "inconsistent_annotation",
    "missing_scale",
    "incomplete_detail",
}

SYSTEM_PROMPT = """You are an expert construction drawing QA inspector. You analyze engineering drawings (mechanical, electrical, structural, civil) for quality issues. You are precise, technical, and thorough.\n\nYou reply with a single JSON object and nothing else. No prose, no explanation, no markdown code fences. The object has exactly one key, "issues", whose value is an array. If you find no issues, reply with {"issues": []}."""

USER_PROMPT_TEMPLATE = """Inspect this engineering drawing page (page {page_num} of {total_pages}) for QA defects. Drawings submitted for review usually contain several defects, so check carefully and report everything you can point to.

OCR Text extracted from this page:
---
{ocr_text}
---

Work through each category in turn, comparing the image against the OCR text:
- missing_tag: Equipment, rooms, pipes or symbols drawn without a tag/label while similar items are tagged.
- dimension_mismatch: Dimensions for the same wall or element that disagree, or notes that give conflicting sizes.
- unlabeled_element: Symbols, components or areas with no identification.
- inconsistent_annotation: Notes or callouts that contradict each other or the drawing.
- missing_scale: No scale bar or scale reference on the page.
- incomplete_detail: Details, sections or sheets referenced in notes (e.g. "DETAIL 5/M-501", "SECTION B-B") that are not shown on this page.

For each defect, quote the specific text or describe the specific element in the description so a reviewer can find it. Only report defects you can point to on this page; do not invent any.

Reply with a single JSON object of exactly this shape:
{{
  "issues": [
    {{
      "issue_type": "<one of: missing_tag, dimension_mismatch, unlabeled_element, inconsistent_annotation, missing_scale, incomplete_detail>",
      "severity": "<one of: low, medium, high>",
      "description": "<what is wrong, quoting the relevant text or element>",
      "location_hint": "<where on the page, e.g. top-right, center, room 204>"
    }}
  ]
}}

Use only the exact lowercase values listed for issue_type and severity."""


def _image_to_base64(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=85)
    return base64.b64encode(buffer.getvalue()).decode("utf-8")


async def analyze_page(
    image: Image.Image,
    ocr_text: str,
    page_num: int,
    total_pages: int,
) -> list[dict]:
    """
    Send a drawing page image + OCR text to the vision model for QA analysis.
    Returns a list of issue dicts, or [] on failure.
    """
    client = AsyncOpenAI(api_key=settings.nvidia_api_key, base_url=settings.llm_base_url)
    image_b64 = _image_to_base64(image)
    user_content = USER_PROMPT_TEMPLATE.format(
        page_num=page_num,
        total_pages=total_pages,
        ocr_text=ocr_text or "(no text extracted)",
    )

    response = await client.chat.completions.create(
        model=settings.llm_vision_model,
        max_tokens=settings.llm_max_tokens,
        temperature=0.2,
        # Enforced JSON. Without it this model reliably ignores "reply with JSON"
        # and answers in markdown prose, which json.loads cannot recover from.
        # json_object mode requires an object, hence the {"issues": [...]} wrapper.
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": user_content},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
                    },
                ],
            },
        ],
    )
    raw = (response.choices[0].message.content or "").strip()

    # Defensive: json_object mode should make fences impossible, but a model that
    # ignores it once should not take the whole page down.
    if raw.startswith("```"):
        lines = raw.split("\n")
        inner_lines = lines[1:]
        if inner_lines and inner_lines[-1].strip() == "```":
            inner_lines = inner_lines[:-1]
        raw = "\n".join(inner_lines).strip()

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("Page %s: model returned unparseable JSON: %s", page_num, raw[:200])
        return []

    if isinstance(payload, list):
        issues = payload  # tolerate a bare array
    elif isinstance(payload, dict):
        issues = payload.get("issues", [])
    else:
        logger.warning("Page %s: unexpected JSON payload type %s", page_num, type(payload))
        return []

    return _normalise(issues, page_num)


def _normalise(issues: list, page_num: int) -> list[dict]:
    """Drop or repair items the model got wrong.

    The previous model followed the schema closely enough that raw output could
    be trusted. This one is smaller and occasionally returns a capitalised
    severity or an issue_type outside the documented set, which the database
    enum rejects at insert time. Filtering here keeps one bad item from failing
    the whole page.
    """
    clean: list[dict] = []
    for item in issues:
        if not isinstance(item, dict):
            continue

        severity = str(item.get("severity", "")).strip().lower()
        if severity not in VALID_SEVERITIES:
            logger.debug("Page %s: dropping unknown severity %r", page_num, item.get("severity"))
            continue

        issue_type = str(item.get("issue_type", "")).strip().lower().replace(" ", "_")
        if issue_type not in VALID_ISSUE_TYPES:
            logger.debug("Page %s: dropping unknown issue_type %r", page_num, item.get("issue_type"))
            continue

        description = str(item.get("description", "")).strip()
        if not description:
            continue

        clean.append({
            "issue_type": issue_type,
            "severity": severity,
            "description": description,
            "location_hint": str(item.get("location_hint", "") or "").strip(),
        })
    return clean
