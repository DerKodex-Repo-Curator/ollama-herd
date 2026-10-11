"""`/api/ps` reported a runner and a digest; herd dropped both on the floor.

Two fields, one construction site (`common/ollama_client.py`), two plans:

* **`runner`** — Ollama 0.40 makes MLX the default on Apple Silicon and chooses
  per model via ``IsMLX()`` == ``ModelFormat == "safetensors"``. So "served by
  Ollama" stopped being one thing, and the question
  ``docs/plans/ollama-mlx-runner-subsystem-decision.md`` is gated on — *which
  runner actually ran this model* — had to be answered by grepping a 5 MB server
  log. It is a field in an endpoint herd already calls every heartbeat.
* **`digest`** — an Ollama upgrade can rewrite the manifest digest of an
  *already resident* model. On 2026-10-10 that doubled both of this fleet's
  models (114 GB, then 156 GB, for two logical models) and `KEEP_ALIVE=-1` meant
  neither copy idled out. Without a digest herd cannot tell two runners for one
  name apart, which is what makes "the resident context for M" ambiguous — see
  ``docs/plans/duplicate-runner-ambiguity.md``.

Both default to `""`, so an older node agent and a pre-0.40 Ollama keep working
unchanged. That is the same discipline `models_available_sizes` and
`max_loaded_models` were introduced with.
"""

import pytest

from fleet_manager.models.node import LoadedModel


class TestFieldsExistAndDegrade:
    def test_defaults_are_empty_not_none(self):
        """A mixed-version fleet must validate without them."""
        m = LoadedModel(name="gpt-oss:120b", size_gb=66.4)
        assert m.runner == ""
        assert m.digest == ""

    def test_older_agent_payload_still_validates(self):
        """Exactly the payload a pre-change node agent sends."""
        m = LoadedModel.model_validate({
            "name": "gemma3:27b", "size_gb": 23.4, "requests_active": 0,
            "parameter_size": "27.4B", "quantization": "Q4_K_M",
            "context_length": 32768,
        })
        assert (m.runner, m.digest) == ("", "")

    def test_round_trips_through_the_heartbeat_shape(self):
        m = LoadedModel(
            name="gemma3:27b", size_gb=23.4, context_length=32768,
            runner="llamacpp", digest="8dda00f4f636",
        )
        assert LoadedModel.model_validate(m.model_dump()).runner == "llamacpp"
        assert LoadedModel.model_validate(m.model_dump()).digest == "8dda00f4f636"


class TestParsedFromApiPs:
    """The real 0.40.2 /api/ps shape, captured from this fleet 2026-10-11."""

    PAYLOAD = {
        "models": [
            {
                "name": "gpt-oss:120b", "model": "gpt-oss:120b",
                "size": 66388009942, "digest": "ad84bf7720de3aac",
                "details": {"format": "gguf", "family": "gptoss",
                            "parameter_size": "116.8B", "quantization_level": "MXFP4"},
                "expires_at": "2319-01-20T19:09:18-08:00",
                "context_length": 131072, "runner": "llamacpp",
            }
        ]
    }

    @pytest.mark.asyncio
    async def test_client_populates_both(self, monkeypatch):
        from fleet_manager.common.ollama_client import OllamaClient

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return TestParsedFromApiPs.PAYLOAD

        c = OllamaClient("http://localhost:11434")

        async def _get(path, *a, **k):
            assert path == "/api/ps"
            return _Resp()

        monkeypatch.setattr(c._client, "get", _get)
        got = await c.get_running_models()
        assert len(got) == 1
        assert got[0].runner == "llamacpp"
        assert got[0].digest == "ad84bf7720de3aac"
        # and the pre-existing fields are untouched
        assert got[0].context_length == 131072
        assert got[0].quantization == "MXFP4"

    @pytest.mark.asyncio
    async def test_missing_fields_do_not_break_older_ollama(self, monkeypatch):
        """Pre-0.40 /api/ps has neither key."""
        from fleet_manager.common.ollama_client import OllamaClient

        payload = {"models": [{"model": "gemma3:27b", "size": 1, "details": {}}]}

        class _Resp:
            def raise_for_status(self): pass
            def json(self): return payload

        c = OllamaClient("http://localhost:11434")

        async def _get(path, *a, **k): return _Resp()

        monkeypatch.setattr(c._client, "get", _get)
        got = await c.get_running_models()
        assert (got[0].runner, got[0].digest) == ("", "")


class TestRunnerIsNotTheMlxPrefix:
    """`runner` describes Ollama's choice; the `mlx:` prefix is herd's routing.

    A model herd routes to `mlx_lm.server` never appears in `/api/ps` at all --
    it is a separate subprocess. So a non-empty `runner` always refers to an
    Ollama-served model, and the two concepts must not be conflated.
    """

    def test_an_ollama_mlx_model_has_no_mlx_prefix(self):
        m = LoadedModel(name="qwen3.8:27b-mlx", size_gb=17.0, runner="mlx")
        assert not m.name.startswith("mlx:")
        from fleet_manager.server.mlx_proxy import is_mlx_model
        assert is_mlx_model(m.name) is False
