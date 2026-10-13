# What to tackle next, and what the evidence says not to

**Status**: **NOT STARTED** (2026-10-13). Written after a triage pass on
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

- **No new `FLEET_*` flags.** None of this needs one.
- **Measure before designing.** This plan exists because two items ranked by
  filed severity collapsed under one query each.
- **Documentation counts as a fix** when the defect is that a healthy signal
  reads as a fault.

---

## Phase 1 — Write down why the median swings (no code)

Record in `docs/troubleshooting.md` and the CLAUDE.md throughput note: the decode
distribution is bimodal, the modes are "running alone" vs "overlapping ~3 others",
prompt size does **not** discriminate them, and the fast-mode share is the stable
reading. Include the measured table above so the next person does not re-derive it.

**Done when** a reader who sees median 77 → 60 can tell in one step whether the
mix moved or the fleet slowed — and the "long-context starvation" issue is
annotated with the measurement that contradicts its title.

## Phase 2 — Stop the CLI import from mutating `os.environ`

Move `load_env_file()` out of module scope into the Typer command bodies (or an
explicit `main()`), so importing the module is side-effect free while both
entry points keep loading `~/.fleet-manager/env` exactly as documented.

**Done when** a test asserts that importing `fleet_manager.cli.server_cli` and
`node_cli` injects **no** `FLEET_*` variable, and both binaries still pick up the
env file — verified by running them, not by reading the code. Note the existing
`tests/test_server/test_cli_env_precedence.py` deliberately reads the CLI as
*text* to dodge this very problem; it should go back to importing once fixed,
which is the real proof.

## Phase 3 — Cover `discovery`, then `rebalancer`

`discovery` first: it is zero-coverage and it is the path that fails when a
network changes, which this fleet has hit (the node-identity gotcha). Then
`rebalancer`'s move / pre-warm decisions.

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
