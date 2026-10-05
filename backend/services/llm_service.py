import asyncio
import base64
import re
import json
import logging
import io
from PIL import Image
import openai
from openai import AsyncOpenAI
from backend.config import get_settings
from backend.services.dimension_check import (
    find_aligned_dimension_conflicts,
    find_dimension_conflicts,
)
from backend.services.ocr_service import ocr_words
from backend.services.rules import run_rules

logger = logging.getLogger(__name__)
settings = get_settings()

# Total tries per page when the reply contains no parseable JSON.
MAX_ATTEMPTS = 3

# Transient API failures (5xx, rate limit, network) are retried with backoff.
API_RETRIES = 4
API_BACKOFF_SECONDS = 2.0

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

USER_PROMPT_TEMPLATE = """Inspect this engineering drawing page (page {page_num} of {total_pages}) for QA defects.

OCR Text extracted from this page:
---
{ocr_text}
---

Check for these categories of defects, comparing the image against the OCR text:
- missing_tag: Equipment, rooms, pipes or symbols drawn without a tag/label while similar items are tagged. An item whose tag appears in the OCR text or on the drawing is tagged; never report it.
- dimension_mismatch: Two different values given for the same wall, run or element.
- unlabeled_element: Symbols, components or areas with no identification.
- inconsistent_annotation: Notes or callouts that give different values for the same thing.
- missing_scale: Report only if the page has neither a scale bar nor any scale note in the OCR text. If a scale is stated anywhere, do not report it.
- incomplete_detail: A detail, section or sheet that a note or callout refers to by its number but that is not shown on this page.

Most pages have few or no defects. Report a defect only if you can quote the exact text from the OCR text, or point to the specific element, that shows it. If a category has no clear defect, report nothing for it; an empty list is a correct answer. Never invent detail numbers, tags or dimensions that do not appear in the OCR text or image, and never report a defect just to fill a category.

Reply with a single JSON object of exactly this shape:
{{
  "issues": [
    {{
      "issue_type": "<one of: missing_tag, dimension_mismatch, unlabeled_element, inconsistent_annotation, missing_scale, incomplete_detail>",
      "severity": "<one of: low, medium, high>",
      "description": "<what is wrong, quoting the exact text or element; use single quotes for any quotation or inch marks inside this string>",
      "location_hint": "<where on the page, e.g. top-right, center, room number>"
    }}
  ]
}}

Use only the exact lowercase values listed for issue_type and severity."""


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, (openai.APIConnectionError, openai.RateLimitError)):
        return True  # APITimeoutError is an APIConnectionError
    return isinstance(exc, openai.APIStatusError) and exc.status_code >= 500


async def _create_with_retry(client: AsyncOpenAI, page_num: int, **kwargs):
    for attempt in range(1, API_RETRIES + 1):
        try:
            return await client.chat.completions.create(**kwargs)
        except Exception as exc:
            if not _is_transient(exc) or attempt == API_RETRIES:
                raise
            delay = API_BACKOFF_SECONDS * 2 ** (attempt - 1)
            logger.warning(
                "Page %s: transient API error (attempt %d/%d), retrying in %.0fs: %s",
                page_num, attempt, API_RETRIES, delay, exc,
            )
            await asyncio.sleep(delay)


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

    # The model occasionally ignores the JSON-only instruction and replies with
    # prose only. Sampling is non-deterministic, so retrying usually recovers.
    for attempt in range(1, MAX_ATTEMPTS + 1):
        response = await _create_with_retry(
            client,
            page_num,
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

        payload = _parse_payload(raw)
        if payload is not None:
            break
        logger.warning(
            "Page %s: model returned unparseable JSON (attempt %d/%d): %s",
            page_num, attempt, MAX_ATTEMPTS, raw[:200],
        )
    else:
        return []

    if isinstance(payload, list):
        issues = payload  # tolerate a bare array
    elif isinstance(payload, dict):
        issues = payload.get("issues", [])
    else:
        logger.warning("Page %s: unexpected JSON payload type %s", page_num, type(payload))
        return []

    kept = _drop_contradicted(_normalise(issues, page_num), ocr_text, page_num)
    # Deterministic dimension checks; skipped when the model already reported one.
    if not any(i["issue_type"] == "dimension_mismatch" for i in kept):
        kept.extend(find_dimension_conflicts(ocr_text))
        words = await asyncio.to_thread(ocr_words, image)
        kept.extend(find_aligned_dimension_conflicts(words, image.width, image.height))
    return _add_rule_findings(kept, ocr_text)


def _parse_payload(raw: str):
    """Parse the model reply as JSON, falling back to the first embedded object.

    The model sometimes ignores json_object mode and wraps the JSON in prose
    ("Upon examining the drawing... { "issues": [...] }"). Scan for a JSON
    object inside the text rather than discarding the whole reply. Returns None
    if nothing parseable is found.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for start, ch in enumerate(raw):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(raw, start)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and "issues" in obj:
            return obj
    return _salvage_issues(raw)


# Drawing text is full of inch marks (37.4"), which the model often leaves
# unescaped inside description strings, making the JSON invalid. Pull each issue
# out field by field instead, tolerating stray quotes inside the text values.
_ISSUE_RE = re.compile(
    r'"issue_type"\s*:\s*"(?P<issue_type>[^"]*)"\s*,\s*"severity"\s*:\s*"(?P<severity>[^"]*)"\s*,\s*'
    r'"description"\s*:\s*"(?P<description>.*?)"\s*,\s*"location_hint"\s*:\s*"(?P<location_hint>.*?)"\s*\}',
    re.DOTALL,
)


def _salvage_issues(raw: str):
    items = [m.groupdict() for m in _ISSUE_RE.finditer(raw)]
    return {"issues": items} if items else None


# Equipment-style tags such as AHU-1, P-101A, FCU2.
_TAG_RE = re.compile(r"\b[A-Z]{1,5}-?\d+[A-Z]?\b")
# "SCALE: ...", "NTS", or an explicit ratio such as 1/4" = 1'-0".
_SCALE_RE = re.compile(r"\bscale\b|\bNTS\b|\d\s*[\"']\s*=\s*\d", re.IGNORECASE)


# Tokens that identify what a rule finding is about: sheet ids, detail/section ids, materials.
_RULE_TOKEN_RE = re.compile(
    r"\b(?:[A-Z]{1,2}-?\d{3}|DETAIL\s+\d+|SECTION\s+[A-Z]-[A-Z]|CMU|GYP|CONCRETE|BRICK|STEEL|WOOD)\b"
)


def _add_rule_findings(model_issues: list[dict], ocr_text: str) -> list[dict]:
    """Append deterministic rule findings and drop model findings that repeat them."""
    rule_issues = run_rules(ocr_text)
    if not rule_issues:
        return model_issues
    covered = [
        {t.replace("  ", " ") for t in _RULE_TOKEN_RE.findall(" ".join(r["evidence"]).upper())}
        for r in rule_issues
    ]
    kept = []
    for issue in model_issues:
        mentioned = set(_RULE_TOKEN_RE.findall(issue["description"].upper()))
        if any(mentioned & tokens for tokens in covered):
            logger.debug("Dropping model finding duplicated by a rule: %s", issue["description"][:80])
            continue
        kept.append(issue)
    return kept + rule_issues


def _drop_contradicted(issues: list[dict], ocr_text: str, page_num: int) -> list[dict]:
    """Drop findings the OCR text directly contradicts.

    The model often reports a missing scale when a scale note exists, or an
    "untagged" item whose tag it just quoted from the OCR text. These checks are
    deterministic, so they remove those false positives without costing recall.
    """
    if not ocr_text:
        return issues

    ocr_upper = ocr_text.upper()
    kept: list[dict] = []
    for issue in issues:
        kind = issue["issue_type"]
        if kind == "missing_scale" and _SCALE_RE.search(ocr_text):
            logger.debug("Page %s: dropping missing_scale, OCR text states a scale", page_num)
            continue
        if kind in ("missing_tag", "unlabeled_element"):
            tags = _TAG_RE.findall(issue["description"].upper())
            if any(tag in ocr_upper for tag in tags):
                logger.debug("Page %s: dropping %s, tag %s is in OCR text", page_num, kind, tags)
                continue
        kept.append(issue)
    return kept


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
        if description[:50] in USER_PROMPT_TEMPLATE:
            # The model sometimes parrots the category definitions back as findings.
            logger.debug("Page %s: dropping finding that echoes the prompt", page_num)
            continue

        clean.append({
            "issue_type": issue_type,
            "severity": severity,
            "description": description,
            "location_hint": str(item.get("location_hint", "") or "").strip(),
        })
    return clean
