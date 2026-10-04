"""Check that the real LLM endpoint works for what this app sends it.

Run before a live evaluation:

    python -m backend.evals.preflight

It exercises the exact request shapes the pipeline uses, one at a time, so a
failure says which assumption broke instead of surfacing later as "every answer
could not be verified":

  1. key        NVIDIA_API_KEY is set
  2. reachable  the endpoint answers (a blocked network shows up here)
  3. model      the configured vision model is offered
  4. json       JSON-object mode works for text
  5. sees       the model actually receives and reads an attached image
  6. large      a page-sized image (what a real drawing produces) is accepted

Exit status is non-zero if any check fails. A failed "large" check with a passing
"sees" check means the endpoint caps inline image size: set
ASSISTANT_MODEL_IMAGE_MAX_BYTES=170000 (the app also retries smaller on its own).
"""
import asyncio
import base64
import io
import json
import random
import sys

import openai

from backend.config import get_settings

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"


def _png_b64_with_digit(digit: str) -> str:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.new("RGB", (256, 256), "white")
    ImageDraw.Draw(img).text((70, 40), digit, fill="black", font=ImageFont.load_default(size=180))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


def _drawing_like_jpeg_b64(side: int = 1600, min_b64_bytes: int = 350_000) -> str:
    """A noisy line drawing about the size of a real model-bound page image."""
    from PIL import Image, ImageDraw

    rng = random.Random(7)
    img = Image.new("RGB", (side, int(side * 0.65)), "white")
    d = ImageDraw.Draw(img)
    quality = 85
    while True:
        for _ in range(2500):
            x, y = rng.randrange(img.width), rng.randrange(img.height)
            d.line([(x, y), (x + rng.randrange(-90, 90), y + rng.randrange(-90, 90))], fill=(0, 0, 0), width=1)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        b64 = base64.b64encode(buf.getvalue()).decode()
        if len(b64) >= min_b64_bytes:
            return b64


async def _json_call(client, model: str, content, max_tokens: int = 60) -> dict:
    resp = await client.chat.completions.create(
        model=model, max_tokens=max_tokens, temperature=0,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": content}],
    )
    return json.loads((resp.choices[0].message.content or "").strip())


def _classify(exc: Exception) -> str:
    if isinstance(exc, openai.AuthenticationError):
        return "the key was rejected (401). Check NVIDIA_API_KEY."
    if isinstance(exc, openai.PermissionDeniedError):
        return "access denied (403). The key may lack access to this model."
    if isinstance(exc, openai.NotFoundError):
        return "model not found (404). Check LLM_VISION_MODEL / LLM_BASE_URL."
    if isinstance(exc, openai.RateLimitError):
        return "rate limited (429). Retry shortly."
    if isinstance(exc, openai.APIConnectionError):
        return ("could not connect. If this environment restricts outbound network access, allow the host in "
                "LLM_BASE_URL (default integrate.api.nvidia.com) in the environment's network settings.")
    if isinstance(exc, openai.APIStatusError):
        return f"HTTP {exc.status_code}: {str(exc)[:200]}"
    return f"{type(exc).__name__}: {str(exc)[:200]}"


async def run_checks(client=None) -> list[tuple[str, str, str]]:
    """Returns [(check, status, detail)]. `client` can be injected for tests."""
    settings = get_settings()
    results: list[tuple[str, str, str]] = []

    def add(name, status, detail=""):
        results.append((name, status, detail))

    if not settings.nvidia_api_key and client is None:
        add("key", FAIL, "NVIDIA_API_KEY is not set. Add it as an environment variable (never paste a key into chat or commit it).")
        return results
    if client is None:
        client = openai.AsyncOpenAI(api_key=settings.nvidia_api_key, base_url=settings.llm_base_url, timeout=60)
    add("key", PASS, "set" + ("" if settings.nvidia_api_key.startswith("nvapi-") else " (does not start with nvapi-; check it)"))

    model = settings.llm_vision_model
    try:
        listing = await client.models.list()
        ids = {m.id for m in listing.data}
        add("reachable", PASS, f"{len(ids)} models listed")
        add("model", PASS if model in ids else WARN,
            f"{model} is offered" if model in ids else f"{model} not in the model list (the list may be partial)")
    except Exception as exc:
        add("reachable", FAIL, _classify(exc))
        return results

    try:
        out = await _json_call(client, model, 'Reply with the JSON object {"ok": true}.')
        add("json", PASS if out.get("ok") is True else WARN, f"replied {out}")
    except Exception as exc:
        add("json", FAIL, _classify(exc) + " JSON-object mode is required by the pipeline.")

    sees_ok = False
    try:
        out = await _json_call(client, model, [
            {"type": "text", "text": 'The image shows one large digit. Reply with JSON: {"digit": <the digit as an integer>}.'},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + _png_b64_with_digit("7")}},
        ])
        sees_ok = str(out.get("digit")) == "7"
        add("sees", PASS if sees_ok else FAIL,
            "read the digit from the image" if sees_ok else f"did not read the image correctly (replied {out}); "
            "the model may be ignoring attached images")
    except Exception as exc:
        add("sees", FAIL, _classify(exc))

    big = _drawing_like_jpeg_b64()
    try:
        await _json_call(client, model, [
            {"type": "text", "text": 'Describe the image in one word. Reply with JSON: {"word": "<word>"}.'},
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + big}},
        ])
        add("large", PASS, f"a {len(big) // 1000} KB image was accepted")
    except Exception as exc:
        hint = ""
        if isinstance(exc, openai.APIStatusError) and exc.status_code in (400, 413, 422) and sees_ok:
            hint = (" Small images work, so the endpoint caps inline image size. Set ASSISTANT_MODEL_IMAGE_MAX_BYTES=170000 "
                    "(the app also retries smaller automatically).")
        add("large", FAIL, f"a {len(big) // 1000} KB image failed: {_classify(exc)}{hint}")
    return results


def main() -> int:
    results = asyncio.run(run_checks())
    width = max(len(r[0]) for r in results)
    for name, status, detail in results:
        print(f"{status:4}  {name:{width}}  {detail}")
    failed = any(r[1] == FAIL for r in results)
    print("\nPreflight " + ("FAILED" if failed else "passed"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
