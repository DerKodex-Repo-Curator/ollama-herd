# Ollama's native MLX runner — deciding our MLX subsystem's fate

**Status**: ✅ **RESOLVED — HOLD (2026-10-11).** Phase 1's decision is made and
Phase 2 is implemented. **Phases 3–4 are correctly not run** — they exist only
under *invest*, and the decision is *hold*. All three Phase 1 sub-questions are
answered: compaction (not a bug), the 33 GB premium (withdrawn, not a real
question), and GLM (measured — MLX is the only usable path).

> **The decision: hold.** Keep the `mlx_lm.server` subsystem, stop extending it,
> and do not measure Ollama-MLX feature parity. The deciding evidence is
> `GLM-4.7-Flash`, and it is not close: all-time on this box, Ollama has **30
> completions against 235 requests retried at the 600 s timeout and 12
> failures** (worst 87 minutes), while MLX has **27 completions and 1 failure**
> and answered in 2.93 s for 84 tokens. **89% of Ollama attempts never completed
> normally.**
>
> So this is not dormant capability kept on sentiment — it is the only working
> path for a model this fleet serves, and that reasoning holds for any
> open-source user running GLM. *Hold* costs nothing: nothing is extended,
> nothing removed, no deprecation notice owed.
>
> **What would reopen it:** Ollama fixing `glm4moelite`
> ([ollama#14045](https://github.com/ollama/ollama/issues/14045)), which would
> make Phase 3 worth running.

*Superseded status line, kept for the trail: "PHASE 2 DONE. BLOCKED ON A
DECISION, NOT ON WORK."*
Phase 1's three sub-questions are 2-of-3 resolved — compaction answered (not a
bug), the 33 GB question withdrawn as not real — leaving **one** open: does
`GLM-4.7-Flash-MLX-4bit` justify itself at 26 requests in 86 days, given that
GLM is ~4x too slow on Ollama (OPEN upstream)? If MLX is its only viable path,
the plan resolves to **hold** and ends here. Phases 3–4 need **invest** *and* an
idle machine. *Revised 2026-10-11 after a codebase + usage audit that reordered
it.* This is Phase 5 of
[`ollama-0.32-upgrade-and-mlx-evaluation.md`](ollama-0.32-upgrade-and-mlx-evaluation.md),
executed 2026-07-17, which answered Q1–Q4 but deferred the subsystem decision.

> **The audit changed the question twice.** The first draft asked whether
> Ollama's MLX runner can replace `mlx_lm.server`. A usage audit of our own trace
> DB suggested the prior question was whether *anything* uses `mlx_lm.server`.
> That inference was then corrected: **this is an open-source project, and one
> fleet's usage measures our cost, not the subsystem's value.** The decision
> needs community evidence, which — it turns out — the project already collects.
> Both are below, kept separate, because conflating them is the mistake this
> section exists to not repeat.

---

## What the audit found

### Our fleet's usage — which measures OUR cost, and nothing else

| | |
|---|---|
| MLX requests, all time | **2,816** |
| Ollama requests, all time | **1,550,264** |
| MLX share | **0.18%** |
| of which the April evaluation burst (2026-W16) | **2,782 — 98.8%** |
| MLX requests in the last 30 days | **1**, and it was `mlx:Definitely-Not-A-Real-Model-4bit`, a test |

Per currently-configured server:

| server | requests ever | last served |
|---|---|---|
| `Qwen3-Coder-30B-A3B-Instruct-4bit` (:11441, the compactor) | **207** | **2026-08-21** — 51 days ago |
| `GLM-4.7-Flash-MLX-4bit` (:11442) | **26** | **2026-07-17** — 86 days ago |

Both are `status: healthy` and have been up 6h46m since the last reboot, holding
**33.0 GB** resident (17.0 + 16.0, from `process_memory.children`).

**Context compaction, the canonical justification for the compactor, has never
fired.** `FLEET_CONTEXT_COMPACTION_ENABLED=true` and
`FLEET_CONTEXT_COMPACTION_MODEL` points at it, but every `compact` log line
across every rotated file is the same startup line — *"Context Compactor enabled
(default curator=…)"* — and the model has served nothing since August.

So the standing cost is **2,881 lines** (`mlx_supervisor` 1,264, `mlx_proxy`
1,596, `mlx_client` 21), `mlx` in **36 files**, **203 of 1,804 tests** (11%),
8 test files, a brittle exact `mlx-lm` pin, the `--quantized-kv-start 0`
workaround, `scripts/setup-mlx.sh`, and 33 GB of RAM — against 233 requests, none
in seven weeks.

**That is a statement about our maintenance burden, and it is the only thing
this data supports.** It is not evidence the subsystem is unused — a single
fleet cannot show that, and reasoning from it to the project would be exactly
the recency-and-availability error that produced the first draft.

### The community's usage — which is the question that matters for an OSS project

The project already collects this: `community_telemetry.py` sends one anonymous
payload per herd per day, default-on, with per-model `entries` carrying a raw
`model` name (`model[:160]` in `anonymous_rollup.py`), so an `mlx:` prefix
survives intact. `GET https://ollamaherd.com/api/v1/stats` returns the
aggregate. Measured 2026-09-11 → 2026-10-11:

| | |
|---|---|
| active installs | **20** |
| active devices | **51** |
| total requests | **308,830** |
| requests on `mlx:`-prefixed models | **0** |

The `items` list is capped at 25 entries and its lowest is **14 requests**, so
any `mlx:` usage above roughly 14 requests/30d *would* appear. The absence is
real down to a low floor.

**Three reasons that is weaker evidence than it looks, and they matter:**

1. **The sample is dominated by us.** The top three models are 295,310 of
   308,830 requests (95.6%) and each appears in just **2 herds** — ours is one.
   The other ~18 installs account for roughly **13,520 requests** between them.
   Absence of `mlx:` in a 13.5k-request sample across 18 installs is thin.
2. **Telemetry is opt-outable**, and we cannot know whether MLX operators opt out
   at a different rate.
3. **20 active installs against ~464 downloads/week** means most installs are not
   in this picture at all.

**One signal pointing the other way, which nearly got missed:**
`qwen3.8:27b-mlx` — **51 requests across 2 herds**. That is an MLX model served
**through Ollama** (no `mlx:` prefix, so it routed to Ollama), not through our
subsystem. Weak, but it is direct evidence that *someone else* runs MLX, and that
the path they use is Ollama's native runner. That supports the strategic question
independently of what we do with our own code.

### The asymmetry that should govern the decision

Our MLX subsystem is **opt-in** — `mlx_enabled: bool = False` on both
`ServerSettings` and `NodeSettings` — so every user of it turned it on
deliberately. It is also **published surface**, not an internal detail:
`docs/guides/mlx-setup.md`, **9 of 37** ClawHub skills, the README, and 20
mentions in the configuration reference.

So the costs are not symmetric. Not investing further costs us nothing. Removing
it breaks a deliberate, documented configuration for users we cannot see, and the
telemetry gives a **floor** on their number, never a zero. For an open-source
project that is three distinct decisions, and the plan should not collapse them:

| | meaning |
|---|---|
| **Invest** | make Ollama-native MLX a first-class path; possibly retire ours after |
| **Hold** | keep it working, stop extending it, stop paying for parity measurements |
| **Deprecate** | announce, give a release of warning, provide a migration — never a silent removal |

What the data *does* rule out is spending effort on Phase 3-style feature parity
before someone has chosen among those three.

### What already exists, so the plan stops proposing it

**herd already computes Ollama's own MLX predicate.** `ModelTagMeta.format` is
populated from `/api/tags`, and `decode_parallelism_for` already branches on
`meta.format == "safetensors"` — which *is* Ollama's `IsMLX()`. So "would Ollama
run this model on MLX?" needs no new code and no model pulls. Measured today:

```
router /api/tags, 39 models:  gguf 27 | safetensors 1 | mlx 2 | onnx 3 | mflux 2 | diffusionkit 2 | '' 2
```

**Exactly one safetensors model exists on this fleet** — `x/z-image-turbo:latest`,
an image model — and neither of our two MLX models is in Ollama at all; they are
HF repos served by `mlx_lm.server`. That answers the old Phase 2 as written and
narrows the real question to *importability*.

**There is already a backend-naming convention; do not invent one.**
`ollama_compat.py` synthesises `format="mlx"` for `mlx:`-prefixed models (lines
306, 312, 502) and `fleet_headers(backend="mlx" if … else "ollama")` at line 1016.
`is_mlx_model` / `strip_mlx_prefix` are the existing predicates.

**Traces need no schema change to separate the backends.** `model LIKE 'mlx:%'`
is sufficient — it is how the table above was produced. Do not add a `backend`
column for this.

**The measurement harness exists.** `benchmark_engine.py` has
`discover_fleet`, `send_request`, `worker`, `warmup`, `percentile` and
`build_report`, all driving the router rather than a backend, plus
`benchmark_runner.py`. It has **zero** `mlx` references, so whether it can target
`mlx:` models is itself a thing to check — but a comparison should extend it, not
hand-roll curl loops.

**The surfaces exist.** `mlx_backend_active` is already a health check, and the
dashboard already has a "Node Models" section built to cover every backend. Any
new reporting belongs there.

## Deliberately excluded

- **Re-measuring distributed multi-Mac** (`mlx.launch`, ring/pipeline) — ours,
  Ollama is single-host, documented in
  [`distributed-mlx-inference.md`](distributed-mlx-inference.md).
- **Re-measuring the compactor's `--draft-model` trade** — settled 2026-07-19
  (84 of 2,813 requests overlapped, 3%).
- **`OLLAMA_NEW_ENGINE`** — deleted upstream in PR #16031; the var is gone.
- **Any new `FLEET_*` toggle.** `FLEET_MLX_ENABLED` / `FLEET_NODE_MLX_ENABLED`
  already exist and are legitimate — they gate spawning subprocesses, a side
  effect outside herd. Nothing here needs a second switch, and a "new MLX path"
  behind a flag is exactly the debt this project forbids.
- **Deleting code in this plan.** This plan produces a decision and its evidence.
  Removal is its own change with its own tests.

## Implementation principles

- **Read-only until a phase says otherwise.** July's execution kernel-panicked
  this box; its rules apply verbatim — idle fleet, one measurement at a time,
  check `memory_pressure` and `vm.swapusage` between each, and **a slow load is a
  stop signal**.
- **One lever per window.** Never upgrade Ollama, change herd, and measure in the
  same window.
- **"Keep it" is a result.** Recorded as firmly as a decision to remove.

---

## Phase 1 — Choose invest / hold / deprecate, which gates the rest — ✅ DECIDED: HOLD (2026-10-11)

No code. Two separable questions, and only the operators hold the second.

**1a — What does the community actually do?** Partly answerable now, and worth
improving before deciding. *(Progress 2026-10-11: the aggregate below was
measured; widening it remains a service-side question.)*

- The `/api/v1/stats` aggregate shows **0** `mlx:` requests across 20 installs,
  real to a ~14 req/30d floor — but with our fleet at 95.6% of the sample, the
  evidence outside us covers only ~13,520 requests.
- **Widen it cheaply before concluding.** The stats endpoint caps `items` at 25;
  a server-side count of *distinct herds reporting any* `mlx:` model, or of
  installs with `mlx_enabled`, would answer this far better than a request-share
  ranking and needs no client change. That is a question for the service, not
  herd.
- `qwen3.8:27b-mlx` in 2 herds says someone runs MLX **through Ollama**. Whether
  that is the direction to support is Phase 3's question.

**1b — What is our own dormant capability for?**

1. ~~Compaction is enabled and has **never fired**. Bug, unused feature, or a
   safety net for long sessions that have not occurred? Only the first is
   work.~~ **ANSWERED 2026-10-11 — not a bug.** Compaction fires above
   `context_compaction_budget_tokens`, default **20,000**, unset on this fleet.
   Across **1,504,192** traced requests with a prompt count, only **87** ever
   reached 20K — 0.006% — and on `gpt-oss:120b` (1.38M requests) exactly
   **two** did. The threshold is simply never crossed, so this is the third
   option: a safety net for sessions that have not occurred.

   **But note which requests crossed it**, because it cuts against deprecation:
   17 of the 87 were `mlx:mlx-community/Qwen3-Coder-Next-4bit` (max prompt
   **129,159**) and 62 more were `qwen3-coder:30b` / `-agent` (max 62,227).
   When this fleet was doing heavy agentic coding, prompts *did* exceed the
   budget — on the MLX path. The capability was relevant to a real workload;
   that workload is simply not the current one. An argument for **hold**, not
   for removal.
2. ~~Does `GLM-4.7-Flash-MLX-4bit` justify itself at 26 requests in 86 days?~~
   **ANSWERED 2026-10-11 — yes, it is the only usable path, which is a keep.**
   Measured on `0.40.2` over all-time traces: Ollama **30 completed / 235
   retried at the 600 s timeout / 12 failed** (worst 87 min) against MLX
   **27 completed / 1 failed**. 89% of Ollama attempts never completed
   normally; MLX answered in 2.93 s for 84 tokens the same afternoon. Ollama
   also emits ~6x the output tokens (3,268 vs 556) for comparable work — the
   interleaved-thinking symptom compounding the slowness. **Low request *count*
   was the wrong question**; the right one was whether the alternative works,
   and it does not.

   Attempting the comparison also wedged Ollama into serving `/api/version`
   while unable to start any runner, reproducing the OPEN "watchdog can't
   escalate" issue and requiring the documented full restart.
   2026-10-11 — this was not a real question.** The 33 GB is two
   `mlx_lm.server` children holding weights resident (17.0 + 16.0), which is the
   price of answering without a ~30 s cold load, not waste. On this box it costs
   nothing measurable: **334 GB available of 512**, swap `0.00M`, kernel
   pressure level **1**, and `underutilized_memory` is actively advising that
   the node *could hold more models*. And the small-machine version of the
   concern (25.8% of 128 GB, 51.6% of 64 GB) is not a trade-off either, because
   `FLEET_NODE_MLX_SERVERS` is **already per-node config** — a constrained node
   lists no servers and runs none.

   The narrow residual, which is a different issue: an idle MLX server has been
   observed **externally SIGKILLed on a saturated box** (OPEN in
   `docs/issues.md`, likely OS memory pressure). That is about saturation, not
   about the 33 GB, and this box is nowhere near it.

**Done when** the outcome is recorded as **invest**, **hold** or **deprecate**,
with the reason.

- **Hold** is the cheapest and is the default if 1b has no clear answer: stop
  extending, keep it working, skip Phases 3–4 entirely. Nothing breaks for
  anyone.
- **Deprecate** does *not* mean delete in this plan. It means an announced path:
  a release that warns, a documented migration to Ollama-native MLX, and removal
  no earlier than the release after. The subsystem is opt-in and documented in
  9 of 37 skills; a silent removal is not available.
- **Invest** is the only outcome that needs Phases 3–4.

## Phase 2 — Surface what Ollama chose (shared, and worth doing regardless) — ✅ DONE 2026-10-11

Added `runner` and `digest` to `LoadedModel`, populated at the single
construction site `common/ollama_client.py`, and the node card's **existing**
backend label — previously the hardcoded string `ollama` — now names the runner
when it is not the `llamacpp` default. Surfacing the exception rather than
tagging every chip with its own default, matching how `kv_cache_bloat` prints
"vs configured" only on a mismatch. No new dashboard section, no new field on the
wire beyond the two, no toggle.

**Also Phase 1 of [`duplicate-runner-ambiguity.md`](duplicate-runner-ambiguity.md)
— shipped once, for both.**

This is *confirmation*, not the primary signal: `ModelTagMeta.format` already
predicts the choice for every available model via the same `safetensors` test
Ollama's `IsMLX()` uses. `runner` reports what Ollama actually *did* for loaded
ones, so a divergence between the two means our model of its selection is wrong
— and that model drives `decode_parallelism_for`.

**Verified live.** Both residents report `runner=llamacpp` in `/fleet/status`,
matching Ollama's own `/api/ps` and the `format=gguf` prediction — the control
held, so the field means what we think. Both fields default to `""`, so a
pre-0.40 Ollama and an older node agent are unaffected; six tests in
`tests/test_models/test_loaded_model_runner_digest.py` pin that, including the
real captured 0.40.2 payload and a test that `runner` is never confused with
herd's own `mlx:` routing prefix.

## Phase 3 — Importability, only if Phase 1 chose *invest* — ⏸ NOT RUN (decision was *hold*)

The real coverage question, narrowed by the audit: can Ollama **import** the MLX
weights we already have — `ollama create` from `mlx-community/…` safetensors —
and does `format` then read `safetensors` so `IsMLX()` selects the MLX runner?

Run on the **MacBook Pro**, not the Studio: the Studio serves this fleet's
production traffic and holds 90 GB resident, which is the shape of the July panic.

Then the feature gaps, which the binary already hints at — `strings` on 0.40.2
shows `mlxrunner` **3,636** times with draft/MTP strings present but **no
`kv-bits`, `quantized-kv` or `kv_group_size` strings at all**, and the compactor
runs `--kv-bits 8 --kv-group-size 64 --quantized-kv-start 0`. Confirm properly;
absence of a string is not absence of a feature. Also note Ollama's MLX runner
decodes **one request at a time** (`mlxrunner/runner.go`), which
`decode_parallelism_for` already mirrors — free for the compactor (3% overlap,
measured), not free for interactive serving.

**Done when** each model is classified *importable and MLX-selected* /
*importable but llama.cpp* / *not importable*, with gaps confirmed either way.

## Phase 4 — Decide, per model, and reconcile the docs — ⏸ NOT RUN (Phase 1 decided it)

| outcome | consequence |
|---|---|
| Ollama covers our models | Ollama-native MLX becomes the documented path; **ours enters the announced deprecation above**, never a silent removal — and **`mlx.launch` distributed stays**, which Ollama cannot do |
| Ollama does not cover them (likely if the KV gap is real) | Keep, and record why, so this is not reopened in another five versions |
| Mixed per model | The `mlx:` prefix keeps earning its place for the gap models only |

Either way: **routing remains the herd's value** — a faster Ollama improves the
herd for free — and **distributed multi-Mac stays ours**.

**Done when** `docs/issues.md`'s MLX-runner entry carries a per-model verdict and
CLAUDE.md's `mlx:` gotcha states the current premise. That gotcha carries a
*"not re-measured since, and 0.40 made MLX the default runner, so treat the
comparison as stale rather than settled"* marker added 2026-10-11; this plan
removes it.

---

## Order and dependencies

**Phase 1 gates 3 and 4 and needs no code** — it is a conversation with the data
above, and *hold* is a legitimate terminal state that skips everything after
Phase 2. Phase 2 is independent, cheap, shared with the duplicate-runner plan, and
worth shipping whatever Phase 1 concludes. Phase 3 needs an idle machine and is
the slow part. Phase 4 follows 3.

## Open questions

1. **Why has compaction never fired?** Enabled since at least 2026-10-02 with a
   curator configured. Either sessions never reach the trigger, or the trigger is
   broken — and the second would be a real bug hiding behind an idle subsystem.
2. **Does `ollama create` accept MLX-community safetensors directly**, or need a
   conversion that changes the weights already measured?
3. **Is the Ollama version pinnable?** This premise went stale because Ollama
   auto-updated across five minors unrequested; 0.40 arriving by reboot is the
   third instance. Raised in the July plan, still open.
4. **Should the two idle servers keep running while this is decided?** 33 GB for
   233 lifetime requests is the cheapest reversible lever on the board, and
   `FLEET_NODE_MLX_SERVERS` already controls it with no code change.

## What this plan is not

It is not a deprecation plan, and **the usage data is not permission to delete
2,881 lines.** Two corrections are worth carrying forward, because both were
made during the writing of this plan rather than after:

1. The first draft measured feature parity before asking whether the workload
   existed.
2. The second draft answered "does the workload exist" from **our fleet alone**,
   in an open-source project with ~464 downloads/week. One fleet's traffic is a
   cost measurement, not a usage measurement. The community aggregate is thin and
   dominated by us, so it establishes a floor on unseen usage and never a zero —
   and an opt-in, documented capability used by anyone at all is removed on
   notice, not on a percentage.
