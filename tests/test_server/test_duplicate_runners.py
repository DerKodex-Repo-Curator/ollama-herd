"""One model name, two runners: the ambiguity and the memory it strands.

An Ollama upgrade can rewrite the manifest digest of an **already resident**
model. A runner loaded before the rewrite keeps the old digest; the next request
for that name resolves to the new one, which Ollama treats as a different model
and loads as a second runner. `OLLAMA_KEEP_ALIVE=-1` means neither idles out.

Observed on both of this fleet's models on 2026-10-10, after `0.40.2` arrived
with a reboot and rewrote both tags a minute apart:

```
gemma3:27b     a418f5838eaf -> 8dda00f4f636   20:21:09
gpt-oss:120b   a951a23b46a1 -> ad84bf7720de   20:22:01
```

Resident memory went 114 GB, then 156 GB, for two logical models. **66 GB sat
stranded until a human looked** — every count derived from `models_loaded`
simply read one model higher, so nothing surfaced it.

The correctness half is worse than the memory: `_get_loaded_context` took the
**first** matching entry, an arbitrary order. Both live duplicates happened to
agree on context, so the `num_ctx` pin was right by luck. Had they differed it
would have sent one runner's window for a request the other served, forcing the
reload it exists to prevent — intermittently.
"""

from types import SimpleNamespace

import pytest

from fleet_manager.server.health_engine import HealthEngine
from fleet_manager.server.serializers import (
    distinct_resident_model_names,
    distinct_resident_models,
    resident_context_for,
)


def _lm(name, digest="", size_gb=10.0, ctx=32768):
    return SimpleNamespace(
        name=name, digest=digest, size_gb=size_gb, context_length=ctx,
        parameter_size="", quantization="", requests_active=0, runner="llamacpp",
    )


def _node(*loaded, node_id="bb", online=True):
    return SimpleNamespace(
        node_id=node_id,
        status=SimpleNamespace(value="online" if online else "offline"),
        memory=SimpleNamespace(available_gb=300.0),
        ollama=SimpleNamespace(
            models_loaded=list(loaded), num_parallel=4, max_loaded_models=10,
            models_available_meta={}, models_available_sizes={},
        ),
    )


# The real digests and sizes from the 2026-10-10 incident.
GEMMA_OLD = _lm("gemma3:27b", "a418f5838eaf", 24.2, 32768)
GEMMA_NEW = _lm("gemma3:27b", "8dda00f4f636", 23.4, 32768)
GPTOSS_OLD = _lm("gpt-oss:120b", "a951a23b46a1", 66.4, 131072)
GPTOSS_NEW = _lm("gpt-oss:120b", "ad84bf7720de", 66.4, 131072)


class TestResidentContextRefusesToGuess:
    """Phase 2: the correctness fix this plan exists for."""

    def test_single_runner_answers_normally(self):
        assert resident_context_for("gemma3:27b", _node(GEMMA_NEW)) == 32768

    def test_two_runners_that_agree_are_unambiguous(self):
        """Both live duplicates were this case -- hence 'right by luck'."""
        assert resident_context_for("gemma3:27b", _node(GEMMA_OLD, GEMMA_NEW)) == 32768

    def test_two_runners_that_disagree_return_unknown(self):
        """The dangerous shape: no right single answer exists."""
        a = _lm("gemma3:27b", "aaaa", 24.2, 32768)
        b = _lm("gemma3:27b", "bbbb", 23.4, 131072)
        assert resident_context_for("gemma3:27b", _node(a, b)) == 0

    def test_not_resident_is_also_zero(self):
        """Deliberately the same value: every caller already handles it."""
        assert resident_context_for("absent:7b", _node(GEMMA_NEW)) == 0

    def test_no_ollama_at_all(self):
        assert resident_context_for("x", SimpleNamespace(ollama=None)) == 0
        assert resident_context_for("x", None) == 0

    def test_the_pin_stops_asserting_a_window_when_ambiguous(self):
        """The whole point: ambiguity routes into the existing passthrough."""
        from fleet_manager.models.request import InferenceRequest, RequestFormat
        from tests.test_server.test_streaming import _make_proxy_with_loaded_model

        proxy = _make_proxy_with_loaded_model(
            model_name="gemma3:27b", context_length=32768, context_protection="strip"
        )
        node = proxy._registry.get_node("test-node")
        # second runner for the same name, different context
        node.ollama.models_loaded.append(
            type(node.ollama.models_loaded[0])(
                name="gemma3:27b", size_gb=23.4, context_length=131072,
                digest="bbbb", runner="llamacpp",
            )
        )
        req = InferenceRequest(
            model="gemma3:27b", messages=[{"role": "user", "content": "Hi"}],
            original_format=RequestFormat.OLLAMA,
            raw_body={"model": "gemma3:27b", "messages": []},
        )
        body = proxy._build_ollama_body(req, "test-node")
        assert "num_ctx" not in body.get("options", {})


class TestDedupeForCounts:
    """Phase 3: stop reading one model higher."""

    def test_runners_are_kept_but_not_double_counted(self):
        n = _node(GEMMA_OLD, GEMMA_NEW, GPTOSS_NEW)
        assert len(distinct_resident_models(n)) == 3  # three real processes
        assert distinct_resident_model_names(n) == {"gemma3:27b", "gpt-oss:120b"}

    def test_same_digest_twice_is_one_runner(self):
        """A reporting artifact, not two processes."""
        n = _node(GEMMA_NEW, _lm("gemma3:27b", "8dda00f4f636", 23.4))
        assert len(distinct_resident_models(n)) == 1

    def test_missing_digest_falls_back_to_name(self):
        """Older node agent: pre-existing behaviour, deduped by name."""
        n = _node(_lm("gemma3:27b"), _lm("gemma3:27b"))
        assert len(distinct_resident_models(n)) == 1

    def test_free_slots_does_not_lose_a_slot_to_a_duplicate(self):
        """Uses the real NodeState factory -- serialize_node needs a full node."""
        from fleet_manager.models.node import LoadedModel
        from fleet_manager.server.serializers import serialize_node
        from tests.conftest import make_node

        n = make_node(loaded_models=[("gemma3:27b", 24.2)])
        # a second runner for the same name, as a manifest rewrite produces
        n.ollama.models_loaded = [
            LoadedModel(name="gemma3:27b", size_gb=24.2, digest="a418f5838eaf"),
            LoadedModel(name="gemma3:27b", size_gb=23.4, digest="8dda00f4f636"),
        ]
        n.ollama.max_loaded_models = 10
        d = serialize_node(n)
        # one logical model, so one slot consumed -- not two
        assert d["models_loaded_count"] == 1
        assert d["free_slots"] == 9


class TestDuplicateRunnersCheck:
    """Phase 4: the card that would have caught it in minutes."""

    def test_the_gpt_oss_incident_warns(self):
        recs = HealthEngine()._check_duplicate_runners([_node(GPTOSS_OLD, GPTOSS_NEW)])
        assert len(recs) == 1
        r = recs[0]
        assert r.check_id == "duplicate_runners"
        assert r.severity.value == "warning"      # 66.4 GB reclaimable
        assert r.data["reclaimable_gb"] == pytest.approx(66.4)
        # the non-obvious remediation must be in the card
        assert "twice" in r.fix
        assert "ollama stop" in r.fix

    def test_the_smaller_gemma_duplicate_is_info(self):
        recs = HealthEngine()._check_duplicate_runners([_node(GEMMA_OLD, GEMMA_NEW)])
        assert recs[0].severity.value == "info"   # 23.4 GB reclaimable
        assert recs[0].data["reclaimable_gb"] == pytest.approx(23.4)

    def test_divergent_contexts_are_called_out_explicitly(self):
        a = _lm("gemma3:27b", "aaaa", 24.2, 32768)
        b = _lm("gemma3:27b", "bbbb", 23.4, 131072)
        r = HealthEngine()._check_duplicate_runners([_node(a, b)])[0]
        assert "different" in r.description and "num_ctx" in r.description

    def test_silent_on_a_healthy_fleet(self):
        assert HealthEngine()._check_duplicate_runners([_node(GEMMA_NEW, GPTOSS_NEW)]) == []

    def test_silent_when_digests_are_absent(self):
        """Two entries we cannot tell apart are not evidence of two runners."""
        n = _node(_lm("gemma3:27b"), _lm("gemma3:27b"))
        assert HealthEngine()._check_duplicate_runners([n]) == []

    def test_silent_on_same_digest_repeated(self):
        n = _node(GEMMA_NEW, _lm("gemma3:27b", "8dda00f4f636", 23.4))
        assert HealthEngine()._check_duplicate_runners([n]) == []

    def test_offline_nodes_skipped(self):
        n = _node(GPTOSS_OLD, GPTOSS_NEW, online=False)
        assert HealthEngine()._check_duplicate_runners([n]) == []

    def test_fires_on_what_everything_working_looks_like(self):
        """The standing question: nothing, because this is not a normal state."""
        single = _node(GEMMA_NEW, GPTOSS_NEW, _lm("glm-4.7-flash", "5c61808ebf0d", 26.7))
        assert HealthEngine()._check_duplicate_runners([single]) == []
