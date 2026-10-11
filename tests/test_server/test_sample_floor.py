"""A rate check with no minimum denominator is not measuring a rate.

`ERROR_RATE_THRESHOLD_PCT` is 5%, and the gate was `error_rate_pct < threshold`
with nothing about the sample size. So **one** failure crossed it at any total of
20 or fewer, and the card said "33.3% error rate in the last 24h (1/3 requests
failed)".

Observed live 2026-10-11, by accident: two probe requests for a deliberately
nonexistent MLX model plus one real one all landed on the `mlx-local`
pseudo-node — the only `node_id` MLX traffic carries — so its 24h window was 1
failed of 3. The card read WARNING while the fleet was at **0.0%** across
thousands of requests. Low-traffic backends are the general case here, not a
quirk of that one: `mlx-local` has 233 lifetime requests.

Seventh check in three weeks found firing on a state the system reaches
legitimately.
"""

import pytest

from fleet_manager.server.health_engine import HealthEngine


class TestFloorArithmetic:
    """Derived from the threshold so the two cannot drift."""

    @pytest.mark.parametrize(
        "rate,expected",
        [
            (0.05, 21),   # 5% error rate
            (0.3, 4),     # 0.3 retries/request
            (0.01, 101),  # 1%
            (0.5, 3),     # 50%
        ],
    )
    def test_one_event_cannot_cross_the_threshold_at_the_floor(self, rate, expected):
        n = HealthEngine._min_samples_for_rate(rate)
        assert n == expected
        # the defining property, asserted rather than assumed
        assert (1.0 / n) < rate
        # and it is the *smallest* such n
        assert (1.0 / (n - 1)) >= rate

    def test_the_float_trap_that_defeated_the_first_attempt(self):
        """`1.0 // 0.05` is 19.0, not 20.

        0.05 has no exact binary form, so 1/0.05 computes as 19.999999999999996
        and a plain floor truncates a whole unit. The first version of this fix
        returned 20, at which one failure is exactly 5.0% -- and the gate is
        `< threshold`, so 5.0 is not below 5.0 and the check fired anyway. The
        off-by-one defeated the entire fix while the arithmetic looked right.
        """
        assert 1.0 // 0.05 == 19.0
        assert HealthEngine._min_samples_for_rate(0.05) == 21

    def test_degenerate_threshold_does_not_divide_by_zero(self):
        assert HealthEngine._min_samples_for_rate(0.0) == 1
        assert HealthEngine._min_samples_for_rate(-1.0) == 1


def _rate(node_id, failed, total):
    return {
        "node_id": node_id,
        "total": total,
        "failed": failed,
        "error_rate_pct": round((failed / total) * 100, 1) if total else 0.0,
    }


class TestErrorRateFloor:
    def test_the_live_false_positive_is_now_info_not_warning(self):
        """1 failed of 3 on mlx-local -- the exact card observed 2026-10-11."""
        entry = _rate("mlx-local", failed=1, total=3)
        assert entry["error_rate_pct"] == 33.3
        recs = HealthEngine()._check_error_rates([entry], [entry])
        assert len(recs) == 1
        r = recs[0]
        assert r.severity.value == "info"
        assert r.data["below_sample_floor"] is True
        assert r.data["min_samples"] == 21
        # the card must say WHY it is not an alarm
        assert "too small a sample" in r.description
        # and must not claim the node is broken
        assert "Check connectivity" not in r.fix

    def test_it_is_reported_rather_than_hidden(self):
        """Silence would hide 3-of-3 as effectively as WARNING over-reports 1-of-3."""
        recs = HealthEngine()._check_error_rates(
            [_rate("mlx-local", failed=3, total=3)], [_rate("mlx-local", 3, 3)]
        )
        assert len(recs) == 1
        assert recs[0].data["failed"] == 3
        assert recs[0].data["total"] == 3

    def test_a_real_rate_above_the_floor_still_warns(self):
        entry = _rate("bb", failed=20, total=200)  # 10%
        recs = HealthEngine()._check_error_rates([entry], [entry])
        assert len(recs) == 1
        assert recs[0].severity.value == "warning"
        assert recs[0].data.get("below_sample_floor") is None
        assert "Check connectivity" in recs[0].fix

    def test_exactly_at_the_floor_is_treated_as_a_real_rate(self):
        entry = _rate("bb", failed=3, total=21)  # 14.3%, 21 clears the floor
        recs = HealthEngine()._check_error_rates([entry], [entry])
        assert recs[0].severity.value == "warning"

    def test_one_below_the_floor_is_not(self):
        entry = _rate("bb", failed=3, total=20)
        recs = HealthEngine()._check_error_rates([entry], [entry])
        assert recs[0].severity.value == "info"
        assert recs[0].data["below_sample_floor"] is True

    def test_healthy_nodes_produce_nothing_regardless_of_size(self):
        assert HealthEngine()._check_error_rates([_rate("bb", 0, 3)], []) == []
        assert HealthEngine()._check_error_rates([_rate("bb", 1, 5000)], []) == []


class TestRetryRateFloor:
    def test_one_retry_in_a_tiny_window_is_silent(self):
        """Fleet-wide average: a small denominator only happens after a restart."""
        stats = {"total_requests": 3, "total_retries": 1, "models_with_retries": {}}
        assert HealthEngine()._check_retry_rates(stats) == []

    def test_a_real_retry_rate_still_fires(self):
        stats = {"total_requests": 1000, "total_retries": 400, "models_with_retries": {}}
        recs = HealthEngine()._check_retry_rates(stats)
        assert len(recs) == 1

    def test_zero_requests_still_handled(self):
        assert HealthEngine()._check_retry_rates(
            {"total_requests": 0, "total_retries": 0, "models_with_retries": {}}
        ) == []


class TestNotAppliedWhereItWouldBeWrong:
    """Absolute-count checks are deliberately left alone.

    `_check_stream_reliability` fires on `no_output >= 1` and
    `_check_embed_error_rate` on `failed >= 1`. Those are counts, not rates --
    one failed model load that reads as a refusal is exactly what 0.11.0 made
    visible on purpose, and suppressing it under a denominator would undo that.
    """

    def test_single_no_output_is_still_reported(self):
        rel = {
            "total_requests": 3, "client_disconnected": 0, "incomplete": 0,
            "no_output": 1, "by_model": {"m": {"no_output": 1, "total": 3}},
        }
        recs = HealthEngine()._check_stream_reliability(rel, rel)
        assert any("no_output" in str(r.data) or r.data for r in recs)
