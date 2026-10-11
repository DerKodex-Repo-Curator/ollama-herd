"""Context protection must apply on every request format, not three of four.

`_build_ollama_body` has two branches. The first is gated on
`original_format in (OLLAMA, ANTHROPIC, RESPONSES)` and calls
`_apply_context_protection`, then returns. `openai_compat` sets
`RequestFormat.OPENAI`, so OpenAI-format requests fell through to the builder
below — which called `_apply_thinking_overhead` but **not** the protection.

For `/v1/chat/completions` that meant no `FLEET_NUM_CTX_OVERRIDES` injection, no
resident-context pin, no overflow detection and no strip/upgrade.

Measured 2026-10-11 with `glm-4.7-flash` (configured 32768, no other callers):
**0** context-protection actions over `/v1/chat/completions` against **1** over
`/api/generate`. A cold load on the OpenAI route came up at 131072/slot instead
of 32768 and sat at **48 GB resident for a 19 GB model**.

These tests are parametrized over formats on purpose. The bug was one format
silently missing a call the others got, so asserting it per-format is the only
shape that would have caught it.
"""

import pytest

from fleet_manager.models.request import InferenceRequest, RequestFormat
from tests.test_server.test_streaming import _make_proxy_with_loaded_model

ALL_FORMATS = [
    RequestFormat.OLLAMA,
    RequestFormat.OPENAI,
    RequestFormat.ANTHROPIC,
    RequestFormat.RESPONSES,
]


def _req(fmt, model="gpt-oss:120b", options=None, with_raw=True):
    """Build a request the way each route does.

    OPENAI carries a raw_body too (openai_compat sets raw_body=body), which is
    what made the missing branch non-obvious: the body is there, it just isn't
    the Ollama shape, so the first branch deliberately excludes it.
    """
    raw = {"model": model, "messages": [{"role": "user", "content": "Hi"}]}
    if options is not None:
        raw["options"] = options
    return InferenceRequest(
        model=model,
        messages=[{"role": "user", "content": "Hi"}],
        original_format=fmt,
        raw_body=raw if with_raw else None,
        max_tokens=16,
    )


class TestEveryFormatGetsTheResidentContext:
    @pytest.mark.parametrize("fmt", ALL_FORMATS, ids=[f.value for f in ALL_FORMATS])
    def test_absent_num_ctx_is_pinned_on_every_format(self, fmt):
        proxy = _make_proxy_with_loaded_model(
            context_length=32768, context_protection="strip"
        )
        body = proxy._build_ollama_body(_req(fmt), "test-node")
        assert body["options"]["num_ctx"] == 32768, (
            f"{fmt.value} did not get the resident context pinned"
        )

    @pytest.mark.parametrize("fmt", ALL_FORMATS, ids=[f.value for f in ALL_FORMATS])
    def test_smaller_num_ctx_is_raised_on_every_format(self, fmt):
        proxy = _make_proxy_with_loaded_model(
            context_length=32768, context_protection="strip"
        )
        body = proxy._build_ollama_body(_req(fmt, options={"num_ctx": 2048}), "test-node")
        assert body["options"]["num_ctx"] == 32768


class TestOverrideInjectionOnEveryFormat:
    """The 32768-vs-131072 cold load, as a unit test."""

    @pytest.mark.parametrize("fmt", ALL_FORMATS, ids=[f.value for f in ALL_FORMATS])
    def test_cold_load_gets_the_configured_override(self, fmt):
        # a model that is NOT resident -- the cold-load path, where the override
        # sets the context the runner comes up with
        proxy = _make_proxy_with_loaded_model(
            model_name="something-else:latest",
            context_length=131072,
            context_protection="strip",
        )
        proxy._settings.dynamic_num_ctx = True
        # NOTE the `:latest`.  InferenceRequest normalizes the model name, and
        # the override lookup is `overrides.get(model)` against the *normalized*
        # name -- so a bare "glm-4.7-flash" key never matches.  That is why the
        # reference fleet's FLEET_NUM_CTX_OVERRIDES lists both spellings, and it
        # cost this test a debugging round.  See TestOverrideKeyNormalization.
        proxy._settings.num_ctx_overrides = {"glm-4.7-flash:latest": 32768}
        body = proxy._build_ollama_body(_req(fmt, model="glm-4.7-flash"), "test-node")
        assert body["options"]["num_ctx"] == 32768, (
            f"{fmt.value} cold load missed the override -- this is the 48 GB bug"
        )


class TestPassthroughStillRespectedEverywhere:
    @pytest.mark.parametrize("fmt", ALL_FORMATS, ids=[f.value for f in ALL_FORMATS])
    def test_passthrough_does_not_pin_on_any_format(self, fmt):
        proxy = _make_proxy_with_loaded_model(
            context_length=32768, context_protection="passthrough"
        )
        body = proxy._build_ollama_body(_req(fmt), "test-node")
        assert "num_ctx" not in body.get("options", {})


class TestTheCallIsNotDuplicated:
    """Both branches must call it exactly once, not one branch twice."""

    def test_protection_is_called_once_per_body_build(self, monkeypatch):
        proxy = _make_proxy_with_loaded_model(
            context_length=32768, context_protection="strip"
        )
        calls = []
        real = proxy._apply_context_protection

        def _spy(body, model, node_id):
            calls.append(model)
            return real(body, model, node_id)

        monkeypatch.setattr(proxy, "_apply_context_protection", _spy)
        for fmt in ALL_FORMATS:
            calls.clear()
            proxy._build_ollama_body(_req(fmt), "test-node")
            assert len(calls) == 1, f"{fmt.value} called protection {len(calls)}x"

    def test_thinking_overhead_also_once(self, monkeypatch):
        """It was already in both branches; make sure the fix did not double it."""
        proxy = _make_proxy_with_loaded_model(
            context_length=32768, context_protection="strip"
        )
        calls = []
        real = proxy._apply_thinking_overhead
        monkeypatch.setattr(
            proxy,
            "_apply_thinking_overhead",
            lambda b, m, n: (calls.append(m), real(b, m, n))[1],
        )
        for fmt in ALL_FORMATS:
            calls.clear()
            proxy._build_ollama_body(_req(fmt), "test-node")
            assert len(calls) == 1


class TestOverrideKeyNormalization:
    """A bare model name in FLEET_NUM_CTX_OVERRIDES silently never matches.

    `InferenceRequest` normalizes `glm-4.7-flash` to `glm-4.7-flash:latest`, and
    `_apply_context_protection` looks up `overrides.get(model)` with the
    normalized name. So configuring the bare form alone is a no-op, with no
    warning -- the override simply never applies and the model cold-loads at
    Ollama's default.

    The reference fleet's config happens to list both spellings, which is why
    this has never bitten there. Documented as a test rather than fixed, because
    changing the lookup to normalize both sides is a behaviour change that
    deserves its own commit.
    """

    def test_bare_key_does_not_match(self):
        proxy = _make_proxy_with_loaded_model(
            model_name="other:latest", context_length=131072, context_protection="strip"
        )
        proxy._settings.dynamic_num_ctx = True
        proxy._settings.num_ctx_overrides = {"glm-4.7-flash": 32768}
        body = proxy._build_ollama_body(
            _req(RequestFormat.OLLAMA, model="glm-4.7-flash"), "test-node"
        )
        assert "num_ctx" not in (body.get("options") or {})

    def test_normalized_key_matches(self):
        proxy = _make_proxy_with_loaded_model(
            model_name="other:latest", context_length=131072, context_protection="strip"
        )
        proxy._settings.dynamic_num_ctx = True
        proxy._settings.num_ctx_overrides = {"glm-4.7-flash:latest": 32768}
        body = proxy._build_ollama_body(
            _req(RequestFormat.OLLAMA, model="glm-4.7-flash"), "test-node"
        )
        assert body["options"]["num_ctx"] == 32768
