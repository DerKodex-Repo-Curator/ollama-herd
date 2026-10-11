"""An MLX model nothing serves failed by blaming the network.

Two conditions, both silent before 2026-10-11:

1. **The node's MLX switch set, the router's not.** `FLEET_NODE_MLX_ENABLED`
   spawns subprocesses; `FLEET_MLX_ENABLED` makes the router build `MlxProxy`
   (`app.py`). Set one without the other and the node loads weights, reports the
   servers healthy, and every `mlx:` request 503s. 33 GB held for nothing, and
   `mlx_backend_active` fires INFO on exactly that state because from the node's
   side it *is* active.
2. **Traffic for a model no healthy server hosts.** `_resolve_url` fell back to
   `FLEET_MLX_URL`'s legacy default (`localhost:11440`), so these were sent to
   whatever held that port: **48 traced `404 Not Found` from
   `http://127.0.0.1:11440` plus 15 `All connection attempts failed`** on this
   fleet, every one blaming the network or a wrong server. Had :11440 hosted a
   different model, the wrong model would have answered.

The remediation pattern is `text_embedding_compat._no_native_server`, which
exists because a 2026-10-04 incident told a client to install software it already
had during a one-minute node restart, and lost the client.
"""

from types import SimpleNamespace

import pytest

from fleet_manager.server.health_engine import HealthEngine
from fleet_manager.server.mlx_proxy import MlxProxy, no_mlx_server_reason


def _srv(model, port=11441, status="healthy"):
    return SimpleNamespace(model=model, port=port, status=status)


def _node(node_id="bb", online=True, servers=()):
    from fleet_manager.models.node import NodeStatus
    return SimpleNamespace(
        node_id=node_id,
        status=SimpleNamespace(value="online" if online else "offline")
        if True else None,
        mlx_servers=list(servers),
    )


def _reg(nodes):
    from fleet_manager.models.node import NodeStatus
    for n in nodes:
        n.status = NodeStatus.ONLINE if n.status.value == "online" else NodeStatus.OFFLINE
    return SimpleNamespace(
        get_all_nodes=lambda: nodes,
        get_online_nodes=lambda: [
            n for n in nodes if n.status == NodeStatus.ONLINE
        ],
    )


class TestResolverNoLongerMisroutes:
    """The fix at the root: a resolver's "no" is authoritative."""

    def test_unhosted_model_resolves_to_nothing(self):
        p = MlxProxy("http://127.0.0.1:11440", url_resolver=lambda m: None)
        assert p._resolve_url("some/model") is None

    def test_hosted_model_still_resolves(self):
        p = MlxProxy(
            "http://127.0.0.1:11440",
            url_resolver=lambda m: "http://10.0.0.5:11441" if m == "a/b" else None,
        )
        assert p._resolve_url("a/b") == "http://10.0.0.5:11441"

    def test_legacy_deploy_without_a_resolver_is_untouched(self):
        p = MlxProxy("http://127.0.0.1:11440")
        assert p._resolve_url("anything") == "http://127.0.0.1:11440"

    def test_model_agnostic_callers_keep_the_fixed_url(self):
        """is_healthy / list_models pass None and must not break."""
        p = MlxProxy("http://127.0.0.1:11440", url_resolver=lambda m: None)
        assert p._resolve_url(None) == "http://127.0.0.1:11440"

    def test_a_raising_resolver_still_falls_back(self):
        """A resolver bug is not an answer about the model."""
        def boom(_m):
            raise RuntimeError("registry blew up")
        p = MlxProxy("http://127.0.0.1:11440", url_resolver=boom)
        assert p._resolve_url("x/y") == "http://127.0.0.1:11440"


class TestReasonIsActionable:
    def test_names_what_is_served_so_a_typo_is_correctable(self):
        reg = _reg([_node(servers=[_srv("mlx-community/Qwen3-Coder-30B-A3B-Instruct-4bit")])])
        msg, retryable = no_mlx_server_reason(reg, "mlx:mlx-community/Qwen3-Coder-30B-A3B-Instuct-4bit")
        assert retryable is False
        assert "FLEET_NODE_MLX_SERVERS" in msg
        # the correctly-spelled model is offered back
        assert "Qwen3-Coder-30B-A3B-Instruct-4bit" in msg

    def test_offline_node_that_hosts_it_is_retryable(self):
        reg = _reg([_node("studio", online=False, servers=[_srv("a/b")])])
        msg, retryable = no_mlx_server_reason(reg, "mlx:a/b")
        assert retryable is True
        assert "studio" in msg and "not online" in msg

    def test_configured_but_unhealthy_is_retryable_and_points_at_the_cards(self):
        reg = _reg([_node(servers=[_srv("a/b", status="memory_blocked")])])
        msg, retryable = no_mlx_server_reason(reg, "mlx:a/b")
        assert retryable is True
        assert "not healthy yet" in msg
        assert "mlx_memory_blocked" in msg

    def test_no_node_online_at_all_is_retryable(self):
        reg = _reg([_node(online=False)])
        msg, retryable = no_mlx_server_reason(reg, "mlx:a/b")
        assert retryable is True

    def test_never_says_connection_failed(self):
        """The whole point: stop blaming transport for configuration."""
        reg = _reg([_node(servers=[])])
        msg, _ = no_mlx_server_reason(reg, "mlx:a/b")
        assert "connection" not in msg.lower()


class TestRoutingDisabledCheck:
    def test_fires_when_node_runs_servers_and_router_is_off(self, monkeypatch):
        monkeypatch.delenv("FLEET_MLX_ENABLED", raising=False)
        nodes = [_node(servers=[_srv("a/b", 11441), _srv("c/d", 11442)])]
        recs = HealthEngine()._check_mlx_routing_disabled(nodes)
        assert len(recs) == 1
        r = recs[0]
        assert r.check_id == "mlx_routing_disabled"
        assert r.severity.value == "warning"
        assert len(r.data["servers"]) == 2
        # both remediations offered: enable routing, or stop paying the memory
        assert "FLEET_MLX_ENABLED=true" in r.fix
        assert "FLEET_NODE_MLX_SERVERS" in r.fix

    def test_silent_when_the_router_is_enabled(self, monkeypatch):
        monkeypatch.setenv("FLEET_MLX_ENABLED", "true")
        nodes = [_node(servers=[_srv("a/b")])]
        assert HealthEngine()._check_mlx_routing_disabled(nodes) == []

    def test_silent_when_no_servers_are_configured(self, monkeypatch):
        monkeypatch.delenv("FLEET_MLX_ENABLED", raising=False)
        assert HealthEngine()._check_mlx_routing_disabled([_node(servers=[])]) == []

    def test_offline_nodes_are_not_counted(self, monkeypatch):
        """An offline node is not holding memory we can do anything about."""
        monkeypatch.delenv("FLEET_MLX_ENABLED", raising=False)
        nodes = [_node("away", online=False, servers=[_srv("a/b")])]
        assert HealthEngine()._check_mlx_routing_disabled(nodes) == []


class TestUnservableRequestsCheck:
    HEALTHY = [_node(servers=[_srv("mlx-community/Served-4bit")])]

    def test_fires_on_traffic_to_an_unhosted_model(self):
        counts = {"mlx:mlx-community/Gone-4bit": 12, "gpt-oss:120b": 5000}
        recs = HealthEngine()._check_mlx_requests_unservable(self.HEALTHY, counts)
        assert len(recs) == 1
        r = recs[0]
        assert r.check_id == "mlx_requests_unservable"
        assert r.severity.value == "warning"
        assert "Gone-4bit" in r.description
        assert r.data["unservable"] == {"mlx:mlx-community/Gone-4bit": 12}
        # names what IS served, so a typo is self-correcting
        assert "Served-4bit" in r.description

    def test_a_single_request_is_only_info(self):
        """A one-off probe of a wrong name is not a fault."""
        recs = HealthEngine()._check_mlx_requests_unservable(
            self.HEALTHY, {"mlx:mlx-community/Typo-4bit": 1}
        )
        assert recs[0].severity.value == "info"

    def test_silent_when_every_mlx_model_is_served(self):
        counts = {"mlx:mlx-community/Served-4bit": 900, "gemma3:27b": 100}
        assert HealthEngine()._check_mlx_requests_unservable(self.HEALTHY, counts) == []

    def test_ignores_non_mlx_models_entirely(self):
        counts = {"gpt-oss:120b": 50_000, "nomic-embed-text": 900}
        assert HealthEngine()._check_mlx_requests_unservable(self.HEALTHY, counts) == []

    def test_silent_with_no_trace_data(self):
        assert HealthEngine()._check_mlx_requests_unservable(self.HEALTHY, None) == []
        assert HealthEngine()._check_mlx_requests_unservable(self.HEALTHY, {}) == []

    def test_an_unhealthy_server_does_not_count_as_serving(self):
        nodes = [_node(servers=[_srv("mlx-community/Loading-4bit", status="starting")])]
        recs = HealthEngine()._check_mlx_requests_unservable(
            nodes, {"mlx:mlx-community/Loading-4bit": 9}
        )
        assert len(recs) == 1
