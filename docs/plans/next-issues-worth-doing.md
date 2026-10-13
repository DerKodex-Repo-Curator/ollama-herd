# What to tackle next, and what the evidence says not to

**Status**: ✅ **COMPLETE (2026-10-13).** All three phases implemented and verified. Written after a triage pass on
`docs/issues.md` and a measurement pass against live trace data.

> **Two of the three items I recommended before measuring turned out to have no
> supporting evidence.** That is the main output of this plan: the ranking by
> filed severity and the ranking by current evidence disagree sharply, and the
> severities were set when the symptoms were fresh.

---

## What the measurements changed

### `available_gb` is the wrong ceiling — real design flaw, **no current harm**

Filed `Medium`, "real request failures, narrow + self-healing window", and I had
it as the top pick. Then:

| | |
|---|---|
| rejections attributable to memory elimination, **all time** | **0** |
| traces with `fallback_used` in the last 30 days | **3** (38,826 all-time, so it is historical) |
| `available_gb` sampled 6× over 12 s | 373.9, 373.9, 373.9, 374.1, 374.1, 373.2 |

The documented volatility (17 GB → 445 GB on an idle box) **does not reproduce
today**, and nothing in the trace history shows a request lost to it. It also has
**~20 consumers** — scorer, preloader, queue_manager, four route scorers,
six places in the health engine, both benchmark modules, the dashboard — so
changing the field's meaning is a wide, risky edit in service of a failure that
is not currently happening.

**Do not start here.** If it returns, the signal to watch is a `rejected` trace
whose `error_message` names elimination, and the scorer already skips the memory
check for *resident* models, which is why the window is narrow and self-healing.

**One prerequisite if it is ever picked up:** the obvious fix is to prefer the
capacity learner's smoothed `capacity.ceiling_gb` over the raw psutil number —
but `capacity` is **empty in the live heartbeat right now** (`live capacity: {}`),
so that ceiling would have to be made real first. Check that before designing
around it.

### "Long-context requests starve co-resident decode" — **the wrong diagnosis**

Filed `medium-high (tail latency)`. The fleet's decode distribution really is
bimodal, which looks like supporting evidence: a fast mode at ≥70 tok/s and a
slow one at <45, with the median sitting in the valley between them and swinging
20 tok/s on small mix changes. Measured over 24 h (n=6,554):

| | prompt tokens (median) | completion tokens | TTFT | **overlapping requests** |
|---|---|---|---|---|
| fast mode (≥70 tok/s) | 835 | 193 | 483 ms | **0.77** |
| slow mode (<45 tok/s) | 864 | 207 | 665 ms | **3.42** |

**Prompt sizes are identical.** It is not long context — it is plain concurrency
on a bandwidth-bound box, which CLAUDE.md already documents and measures
(single-stream gemma3 22.5 tok/s, four slots 27.1 — 1.2× of one). That is an
accepted characteristic, not a defect.

**So this issue has no evidence either**, and the thing that *should* happen is
documentation, not admission control — see Phase 1.

### "Ollama native image models can evict LLMs" — `High`, and **dormant**

The last image request on this fleet was **2026-03-30**, 105 image traces all
time, and `image_generations_24h: 0`. The severity is right about what happens
*if* it happens; the probability here is zero until someone generates an image.
Leave it filed, and re-read it before the next image work rather than now.

---

## What the codebase audit changed (2026-10-13)

Three findings, and the first removes most of Phase 1.

### The right statistic already exists, and is already consumed correctly

`trace_store.get_decode_latency_stats` computes **p99 TPOT per `node:model`,
recent window against that model's own 24 h baseline**, and
`_check_decode_degraded` fires at a 2.0x ratio. Its docstring already says why
per-request tok/s is the wrong measure — *"conflates all three and will happily
blame a model for time it spent waiting in a queue — which is exactly how the
2026-07-19 `glm-4.7-flash` investigation lost several hours."*

**So the metric I was about to invent ("fast-mode share") is not needed**, and
adding it would put a second throughput statistic beside the one this project
already chose and documented. Phase 1 becomes a pointer to existing material
plus one measurement, not a new concept.

**And I verified the existing check is not exposed to the bimodality**, by
replaying its own comparison across 48 half-hour windows:

| | |
|---|---|
| worst recent-p99 / baseline-p99 ratio observed | **1.33x** |
| firing threshold (`DECODE_DEGRADED_RATIO`) | 2.0x |
| margin | **1.51x** |
| windows that would have fired | **0** |
| times it has ever fired, any log | **0** |

Worth recording as a watch item rather than a change: p99 TPOT varies more
hour-to-hour (49% of mean) than median tok/s does (34%), so the margin is
thinner than the threshold suggests. It holds today; it is not enormous.

**One correction to my own framing.** I had assumed the bimodality was an
artifact of choosing tok/s. It is not — binned both ways, the two modes are
present in TPOT as well (a dominant mode at 10–15 ms and a shoulder at 25–30 ms).
The phenomenon is real; what was wrong was reporting its *median*.

### Phase 2 has an existing hook, and a constraint not to break

Both CLIs are structurally identical: `load_env_file()` at module scope on line
15, then `app = typer.Typer(...)`, then `@app.callback(invoke_without_command=True)
def start(...)`. **That callback is the hook** — Typer runs a group callback
before any subcommand, so moving the call into it covers `herd`, `herd mlx pull`,
`herd mlx list` and `herd mlx serve` without inventing a pattern.

**The constraint:** the module-scope placement is deliberate and documented —
*"`ServerSettings` is only imported inside `start()` below, so calling
`load_env_file()` here at module-import time is early enough."* The fix must keep
the env file loading **before** any settings class instantiates, and should
update that comment rather than delete it.

**And a correction to this plan:** the test I referenced is
`tests/test_models/test_cli_env_precedence.py`, not `test_server/`. Its claim
stands though — lines 51–96 read the CLI files with `read_text()` specifically to
avoid importing them, which is the dodge the fix would make unnecessary.

### Phase 3 needs no new harness

`common/discovery.py` imports `zeroconf` (a declared dependency) and uses
`AsyncZeroconf`, `AsyncServiceBrowser` and `AsyncServiceInfo`. Two established
mocking patterns already exist to follow: `monkeypatch.setattr` for a shelled-out
dependency (`test_backend_clients.py`, for `lsof`) and `MagicMock` / `AsyncMock`
for an async client (`test_mlx_client.py`). Use those rather than adding a
fixture layer.

---

## What does have evidence

### 1. The bimodal decode distribution is being misread — document it

It is not a bug and it does not need fixing, but it **is** actively misleading:
the hourly median moves between 42 and 79 on a healthy fleet, and I reported
those swings as a possible regression on two consecutive days before measuring
the cause. The fast-mode *share* is the stable statistic (47–61% in every full
hour, 53% overall); the median is the unstable one because it sits in the valley.

**Cheapest high-value work in this plan.** A future session will otherwise
re-derive it, or worse, act on it.

### 2. `cli.server_cli` import injects the operator's env — measured, reproducible

```
importing cli.server_cli injected 28 FLEET_* vars
e.g. FLEET_ANTHROPIC_AUTO_CLEAR_TOOL_USES_TRIGGER_TOKENS, FLEET_AUTO_PULL,
     FLEET_CONTEXT_COMPACTION_ENABLED, …
```

`load_env_file()` runs at module scope, so *importing* the CLI mutates
`os.environ` for the whole process. It has already cost once: a test that
imported the module caused four unrelated failures. **This is the same class as
the `_context_protection_events` leak fixed 2026-10-11** — order-dependent state
that makes an unrelated change look guilty — and that one sat latent for months
before going reliably red in a single afternoon.

### 3. `common/discovery.py` has zero tests; `server/rebalancer.py` is thin

Narrowed on 2026-10-13 from four modules to two. `discovery` is mDNS advertise
and browse — the mechanism by which a node finds the router at all, and the thing
that silently fails on a changed network. `rebalancer` is exercised indirectly by
two route tests but has nothing covering the move / pre-warm decisions, which is
where its logic lives.

---

## Deliberately excluded

- **The Aug-22 step change.** Highest filed severity and the oldest unexplained
  item, but the operators declined investigating it and CLAUDE.md carries an
  explicit do-not-re-propose list. Not reopened here.
- **Accepted costs**, documented as such and not defects: the ~3.7 GB embedding
  retention, llama-server's unreported 8 GiB prompt cache, pinning models
  forever, the idle-MLX SIGKILL (mitigated), the DiffusionKit cleanup crash,
  dashboard inline HTML, and the three Known Limitations.
- **`#7` (macOS 27 brew).** Unreproduced, and the remaining lead is a stray
  `py3.13` `.pth` in that machine's python@3.12 breaking `import json` — an
  environment, not a formula. Two clean fresh-user installs since.

## Implementation principles

- **No new `FLEET_*` flags.** None of this needs one, and the audit confirmed
  none of the three phases has a side effect outside herd that would justify an
  opt-in — which is the only category this project allows.
- **No second metric, no second code path.** The audit's main finding is that the
  throughput statistic was already chosen and documented; adding "fast-mode
  share" beside it would have been exactly the debt this rule forbids. Same
  reasoning for Phase 2: the fix uses the Typer callback that already exists
  rather than adding an initialization layer, and Phase 3 uses the two mocking
  patterns already in the suite rather than a third.
- **Measure before designing.** This plan exists because two items ranked by
  filed severity collapsed under one query each.
- **Documentation counts as a fix** when the defect is that a healthy signal
  reads as a fault.

---

## Phase 1 — Point at the statistic that already exists (docs only) — ✅ DONE 2026-10-13

Smaller than first drafted, because the audit found the measure is already
chosen. Record in `docs/troubleshooting.md` and the CLAUDE.md throughput note:

- The decode distribution is bimodal, and the modes are **"running alone" vs
  "overlapping ~3 others"** — 0.77 vs 3.42 concurrent requests, with **identical
  prompt sizes** (835 vs 864 tokens median).
- Therefore the per-request **median tok/s is unstable by construction** — it
  sits in the valley between the modes and swings 42–79 on a healthy fleet.
- **The answer is not a new metric.** Use `get_decode_latency_stats`' p99 TPOT
  against a model's own baseline, which is what `decode_degraded` already does
  and what that function's docstring already argues for.
- Note the measured margin (1.33x observed against a 2.0x threshold) so a future
  reader knows the check is safe *and* by how much.

**Done when** a reader who sees median 77 → 60 can tell in one step whether the
mix moved or the fleet slowed, and is pointed at the existing check rather than
inventing a statistic — plus the "long-context starvation" issue carries the
measurement that contradicts its title (**done 2026-10-13**).

## Phase 2 — Stop the CLI import from mutating `os.environ` — ✅ DONE 2026-10-13

Move `load_env_file()` from module scope into the **existing**
`@app.callback(invoke_without_command=True)` in each CLI — Typer runs a group
callback before any subcommand, so one line per file covers every entry point and
no new pattern is introduced.

**Preserve the ordering the current comment defends:** the env file must load
before any settings class instantiates, which holds because `ServerSettings` is
imported *inside* `start()`. Update that comment to say where the call moved and
why the guarantee still holds; do not delete it.

**Done when** a test asserts that importing `fleet_manager.cli.server_cli` and
`node_cli` injects **no** `FLEET_*` variable, and both binaries still pick up the
env file — verified by running them, not by reading the code. Note that
`tests/test_models/test_cli_env_precedence.py` reads the CLI files with
`read_text()` (lines 51–96) specifically to avoid importing them; those checks
should become real imports once fixed, which is the actual proof.

## Phase 3 — Cover `discovery`, then `rebalancer` — ✅ DONE 2026-10-13

`discovery` first: it is zero-coverage and it is the path that fails when a
network changes, which this fleet has hit (the node-identity gotcha). Then
`rebalancer`'s move / pre-warm decisions.

Follow the two mocking patterns already in the suite rather than adding a layer:
`monkeypatch.setattr` for a shelled-out dependency (as `test_backend_clients.py`
does for `lsof`) and `MagicMock` / `AsyncMock` for an async client (as
`test_mlx_client.py` does). `discovery` wraps `zeroconf`'s `AsyncZeroconf`,
`AsyncServiceBrowser` and `AsyncServiceInfo`, all of which that second pattern
covers.

**Done when** both have tests of their own rather than incidental coverage, and
`docs/issues.md` item 10 can close rather than narrow again.

---

## Order and dependencies

Phase 1 is documentation and unblocks nothing, but it is the one most likely to
save a future session from acting on a non-problem — do it first because it is
cheap, not because it is urgent. Phase 2 is self-contained and has a clear test.
Phase 3 is the largest and least urgent.

## Open questions

1. **Is `capacity.ceiling_gb` supposed to be populated?** It is empty in the live
   heartbeat, and `queue_manager` already does
   `min(available_gb, capacity.ceiling_gb)` when it is set — so a dormant
   capacity learner means that guard never engages. Worth checking on its own
   merits, separately from `available_gb`.
2. **Does the fast/slow split hold on a second node?** Everything here is from a
   single M3 Ultra. The M4 Max has not been online during these windows.

## What this plan is not

It is not a ranked backlog of everything open — 16 entries remain and most are
accepted costs. It is the subset where a measurement either supported acting or
explicitly argued against it, and the second category is the more useful half.


---

## Outcome (2026-10-13)

**Phase 1** — `docs/troubleshooting.md` gained a "Throughput *dropped* — read the
right statistic" runbook, and CLAUDE.md the measured margin. No new metric: it
points at `get_decode_latency_stats`' p99 TPOT and the `decode_degraded` check,
which the audit showed were already the right answer.

**Phase 2** — `load_env_file()` moved into each CLI's existing Typer callback,
**before** `server_cli`'s subcommand early-return since `herd mlx pull` reads
settings too. **28 injected `FLEET_*` variables → 0.** Verified by running the
binaries: the router still resolves `FLEET_NUM_CTX_OVERRIDES` from the env file
after a restart and the `num_ctx` pin still fires, which needs
`FLEET_DYNAMIC_NUM_CTX` from the same file.

The test that could not exist before now does — `test_cli_env_precedence.py`
imports both CLIs in a **subprocess** and asserts zero injection. It is a
subprocess because the damage is irreversible in-process: once the variables are
in `os.environ`, nothing can tell whether this import or an earlier one put them
there. And it is paired with a placement assertion, because the import test
passes *vacuously* on a machine with no env file — which is precisely where the
bug was invisible.

**Phase 3** — 9 tests for `discovery`, 15 for `rebalancer`, closing
`docs/issues.md` item 10 entirely (all four modules now covered). Both use
mocking patterns already in the suite.

One test was rewritten mid-implementation: the first version of discovery's
success-path test patched so much of `discover()` that it proved nothing and
ended up asserting a `KeyError` despite its name. The honest version captures the
handler that `discover()` passes to `AsyncServiceBrowser` and invokes it the way
zeroconf would, so the production closure writes into the production locals.

**1921 tests** (from 1894), ruff clean, verified in multiple orders for the
suite-order-leak class.
