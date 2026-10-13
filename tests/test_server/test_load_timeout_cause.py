"""A model-load timeout has two causes with opposite remediations.

`model_load_timeout` was written for eviction: a model keeps getting pushed out
and re-requested, and reloading outruns the request timeout. Fix: `keep_alive: -1`
or a bigger hot-model cap.

**There is a second cause with the identical symptom**, and the check asserted
the first one. Ollama can reach a state where it answers `/api/version` and
`/api/ps` in under a millisecond while being unable to start *any* new runner.
Reproduced 2026-10-11 by repeatedly loading `glm-4.7-flash`: a direct non-herd
request for `gemma3:27b` then hung the full 300 s, with **350 GB free, zero swap
and kernel pressure normal**. The card read *"the model is likely being evicted
from memory"* and advised `keep_alive: -1` — already set — and
`OLLAMA_MAX_LOADED_MODELS`, already 10.

The discriminator needs no new data: **two or more distinct models timing out
while memory is ample** cannot be eviction, which requires pressure and would not
pick on everything at once.

And the check still does not act. The "Ollama watchdog" that probed and killed
stuck runners was removed 2026-04-23 after its escalation to a full `ollama serve`
restart wiped every pinned model — triggered by a probe that read a 400 from an
embedding model as a stuck runner.
"""

from types import SimpleNamespace

from fleet_manager.server.health_engine import HealthEngine


def _timeouts(counts: dict[str, int]):
    """The real shape `trace_store.get_model_timeouts_24h` returns."""
    by_model = {m: {"count": c, "nodes": ["bb"]} for m, c in counts.items()}
    return {
        "total_count": sum(counts.values()),
        "by_node": {"bb": {"count": sum(counts.values())}},
        "by_model": by_model,
    }


def _node(available_gb):
    from fleet_manager.models.node import MemoryPressure, NodeStatus
    return SimpleNamespace(
        node_id="bb",
        status=NodeStatus.ONLINE,
        memory=SimpleNamespace(
            available_gb=available_gb, total_gb=512.0, pressure=MemoryPressure.NORMAL
        ),
    )


class TestWedgedOllama:
    """Several models, plenty of memory -> not eviction."""

    def test_names_the_wedge_not_eviction(self):
        t = _timeouts({"gemma3:27b": 4, "glm-4.7-flash:latest": 5})
        recs = HealthEngine()._check_model_load_timeouts(t, t, [_node(350.0)])
        assert recs, "should still fire"
        d = recs[0].description
        assert "not eviction" in d
        assert "unable to start any new runner" in d
        # the trap that made this hard to diagnose
        assert "liveness probe means nothing" in d

    def test_remediation_is_the_restart_not_keep_alive(self):
        t = _timeouts({"gemma3:27b": 4, "glm-4.7-flash:latest": 5})
        fix = HealthEngine()._check_model_load_timeouts(t, t, [_node(350.0)])[0].fix
        assert "Electron parent" in fix
        assert "keep_alive" not in fix, "keep_alive was already set during the incident"
        # and it must be explicit that herd will not do it automatically
        assert "removed 2026-04-23" in fix

    def test_offers_a_direct_confirmation_step(self):
        """The operator should be able to verify before restarting anything."""
        t = _timeouts({"a:1b": 3, "b:2b": 3})
        fix = HealthEngine()._check_model_load_timeouts(t, t, [_node(300.0)])[0].fix
        assert "localhost:11434" in fix and "non-herd" in fix


class TestEvictionStillDiagnosedAsBefore:
    def test_single_model_keeps_the_eviction_wording(self):
        t = _timeouts({"gemma3:27b": 4})
        recs = HealthEngine()._check_model_load_timeouts(t, t, [_node(350.0)])
        assert "evicted from memory" in recs[0].description
        assert "keep_alive" in recs[0].fix

    def test_several_models_under_memory_pressure_is_still_eviction(self):
        """Tight memory is exactly when eviction DOES explain several models."""
        t = _timeouts({"gemma3:27b": 4, "glm-4.7-flash:latest": 5})
        recs = HealthEngine()._check_model_load_timeouts(t, t, [_node(10.0)])
        assert "evicted from memory" in recs[0].description
        assert "not eviction" not in recs[0].description


class TestUnchangedBehaviour:
    def test_below_the_count_floor_nothing_fires(self):
        t = _timeouts({"gemma3:27b": 2})
        assert HealthEngine()._check_model_load_timeouts(t, t, [_node(350.0)]) == []

    def test_resolved_when_nothing_recent(self):
        t = _timeouts({"gemma3:27b": 4})
        quiet = {"total_count": 0, "by_model": {}}
        recs = HealthEngine()._check_model_load_timeouts(t, quiet, [_node(350.0)])
        assert recs and recs[0].severity.value == "info"

    def test_it_never_kills_anything(self):
        """Regression guard: this check diagnoses, it does not remediate.

        The watchdog it replaces escalated to a full `ollama serve` restart and
        wiped every pinned model. Nothing here should ever shell out.
        """
        import inspect
        src = inspect.getsource(HealthEngine._check_model_load_timeouts)
        for forbidden in ("subprocess", "pkill", "os.system", "Popen"):
            assert forbidden not in src, f"{forbidden} must not appear in a health check"
