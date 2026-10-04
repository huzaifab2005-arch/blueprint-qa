"""The preflight's decision logic, against a stub endpoint (no network)."""
import types

import httpx
import openai
import pytest

from backend.evals import preflight

pytestmark = pytest.mark.asyncio


def _resp(content: str):
    return types.SimpleNamespace(choices=[types.SimpleNamespace(message=types.SimpleNamespace(content=content))])


class StubClient:
    def __init__(self, *, reads_image=True, size_cap=None, models=("meta/llama-3.2-11b-vision-instruct",), fail_list=None):
        self.reads_image, self.size_cap, self.fail_list = reads_image, size_cap, fail_list
        self.models = types.SimpleNamespace(list=self._list)
        self.chat = types.SimpleNamespace(completions=types.SimpleNamespace(create=self._create))
        self._models = models

    async def _list(self):
        if self.fail_list:
            raise self.fail_list
        return types.SimpleNamespace(data=[types.SimpleNamespace(id=m) for m in self._models])

    async def _create(self, **kw):
        content = kw["messages"][0]["content"]
        if isinstance(content, str):
            return _resp('{"ok": true}')
        url = content[1]["image_url"]["url"]
        if self.size_cap and len(url) > self.size_cap:
            req = httpx.Request("POST", "http://x")
            raise openai.BadRequestError("payload too large", response=httpx.Response(400, request=req), body=None)
        if "digit" in content[0]["text"]:
            return _resp('{"digit": 7}' if self.reads_image else '{"digit": 3}')
        return _resp('{"word": "lines"}')


async def _run(**kw):
    return {name: (status, detail) for name, status, detail in await preflight.run_checks(StubClient(**kw))}


async def test_everything_works():
    r = await _run()
    assert all(status == "PASS" for status, _ in r.values()), r


async def test_model_that_ignores_images_is_a_failure():
    r = await _run(reads_image=False)
    assert r["sees"][0] == "FAIL" and "ignoring attached images" in r["sees"][1]


async def test_inline_image_size_cap_is_diagnosed_with_the_fix():
    r = await _run(size_cap=180_000)
    assert r["sees"][0] == "PASS" and r["large"][0] == "FAIL"
    assert "ASSISTANT_MODEL_IMAGE_MAX_BYTES=170000" in r["large"][1]


async def test_blocked_network_is_diagnosed():
    req = httpx.Request("GET", "http://x")
    r = await _run(fail_list=openai.APIConnectionError(request=req))
    assert r["reachable"][0] == "FAIL" and "network" in r["reachable"][1]
    assert "json" not in r                                   # stops early: nothing else can work


async def test_unknown_model_warns_rather_than_fails():
    r = await _run(models=("some/other-model",))
    assert r["model"][0] == "WARN"


async def test_missing_key_fails_first(monkeypatch):
    from backend.config import get_settings
    monkeypatch.setattr(get_settings(), "nvidia_api_key", "")
    results = await preflight.run_checks()
    assert results[0][:2] == ("key", "FAIL") and len(results) == 1
