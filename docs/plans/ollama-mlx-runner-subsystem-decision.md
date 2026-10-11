# Ollama's native MLX runner — deciding our MLX subsystem's fate

**Status**: **NOT STARTED.** This is Phase 5 of
[`ollama-0.32-upgrade-and-mlx-evaluation.md`](ollama-0.32-upgrade-and-mlx-evaluation.md),
which was executed 2026-07-17 and answered Q1–Q4 but explicitly deferred the
subsystem decision. It is written now because the answer that deferred it has
changed.

---

## Why this plan exists now and not in July

July's Q4 was the decisive question — Ollama's MLX **model coverage** — and the
answer was that the question did not yet apply: *"MLX is NOT active. 0 `mlx` in a
5 MB log; `ggml_metal_init`/`llama_model_loader` throughout. All gains are
llama.cpp, MLX still dormant."* The strategic inversion recorded then was that
Ollama's llama.cpp (77.8 tok/s) already beat our `mlx_lm.server` (59) **with
Ollama's MLX switched off**, leaving distributed multi-Mac as the subsystem's
only clear justification.

Three things changed:

1. **Ollama `0.40` makes MLX the default runner on Apple Silicon.** Selection is
   per model via `IsMLX()`, which is exactly `ModelFormat == "safetensors"` — not
   a flag, so there is nothing to switch on.
2. **`/api/ps` now reports a `runner` field** (`llamacpp` / MLX) per loaded
   model. July's Q4 had to be answered by grepping a 5 MB server log for `mlx`;
   it is now a field in an API herd already calls on every heartbeat.
3. **We are on `0.40.2`**, which arrived with a reboot on 2026-10-10 and was not
   requested.

So the decisive unknown is now cheaply measurable, and the subsystem it governs
is large.

## What is actually at stake

| | |
|---|---|
| `node/mlx_supervisor.py` | 1,264 lines — spawn, health-check, auto-restart, memory gate, orphan reap, crash-rate quarantine, `mlx.launch` wrapping |
| `server/mlx_proxy.py` | 1,596 lines — OpenAI↔Anthropic translation, per-URL client pool, reasoning-field surfacing |
| `node/mlx_client.py` | 21 lines |
| spread | `mlx` appears in **36 files** under `src/` |
| tests | **203 of 1,804** (11%) are MLX-related |
| ops | `scripts/setup-mlx.sh`, an exact `mlx-lm` pin that has broken on upgrades before, and the `--quantized-kv-start 0` workaround |
| memory | two `mlx_lm.server` children at ~17 GB each |

This is the largest optional subsystem in the project, and its premise is a
claim about Ollama that is now five minor versions stale.

## What the audit changed

Three findings moved work out of this plan before it was written.

**The measurement is already one field away.** `/api/ps` returns `runner` and
herd reads that endpoint every heartbeat, in exactly one place
(`common/ollama_client.py:50`). Adding it to `LoadedModel` is the same one-site
change that
[`duplicate-runner-ambiguity.md`](duplicate-runner-ambiguity.md) needs for
`digest`. **Do both in one edit; neither plan should do it alone.**

**Two of our three MLX justifications are already settled in the repo and need
no re-litigation.** Distributed multi-Mac (`mlx.launch`, `backend: ring`,
`pipeline`) is ours — Ollama is single-host, documented in
[`distributed-mlx-inference.md`](distributed-mlx-inference.md). And the
compactor's `--draft-model` trade was measured on 2026-07-19 (84 of 2,813
requests ever overlapped — 3%), so serialisation is not a cost for that
workload. Do not re-measure either.

**A feature gap is visible without running anything.** `strings` on the 0.40.2
binary: `mlxrunner` appears **3,636** times, and draft/MTP/speculative strings
are present — but **no `kv-bits`, `quantized-kv` or `kv_group_size` strings at
all.** Our compactor runs `--kv-bits 8 --kv-group-size 64 --quantized-kv-start
0`. If that absence holds up, the compactor cannot move as configured, which
narrows the decision before a single model is pulled.

## Deliberately excluded

- **Re-measuring distributed multi-Mac or the draft-model trade.** Settled, above.
- **Deprecating anything in this plan.** This plan produces a decision and the
  evidence for it. If the decision is to remove code, that is its own change
  with its own tests — bundling them is how a measurement becomes a commitment
  before it is a result.
- **`OLLAMA_NEW_ENGINE`.** It no longer exists; Ollama deleted both CGO engines
  in PR #16031. The July plan's "one untested lever" is gone.
- **Chasing `glm-4.7-flash` performance.** Separate OPEN upstream issue.

## Implementation principles

- **Read-only until a phase says otherwise.** The July execution kernel-panicked
  this box by letting a memory-heavy test fall through to a real load. Its rules
  apply here verbatim: idle fleet, one measurement at a time, check
  `memory_pressure` and `vm.swapusage` between each, and **a slow load is a stop
  signal, not a curiosity**.
- **One lever per window.** Do not upgrade Ollama, change herd and measure in the
  same window.
- **The null result is a result.** "Coverage is too narrow / the KV gap is real,
  keep the subsystem" ends this plan successfully and should be recorded as
  firmly as a decision to remove code.

---

## Phase 1 — Surface the runner herd is already told about

Add `runner` and `digest` to `LoadedModel`, populate both at
`common/ollama_client.py:50`, and show `runner` on the node card beside each
loaded model. Shared with the duplicate-runner plan.

**Why first:** it converts the central question from a log-grep into fleet state,
and it is the only code change in this plan's measurement phases. After it, "is
this model on MLX?" is answerable for every model on every node, continuously,
including on the MacBook Pro.

**Done when:** `/fleet/status` reports `runner` per loaded model, and the two
current residents read `llamacpp` (both GGUF — this is the control, and if they
read anything else the field's meaning is not what we think).

## Phase 2 — Establish coverage, which is the question that decides everything

For each model we currently serve via `mlx_lm.server`, determine whether Ollama
can run it on MLX at all:

1. Does a safetensors build exist in Ollama's registry, or can `ollama create`
   import the MLX-community weights we already have on disk?
2. Does `IsMLX()` select the MLX runner for it — i.e. does `/api/ps` report a
   non-`llamacpp` runner after loading?
3. Does it answer correctly through herd's existing OpenAI and Anthropic routes?

**Run on the MacBook Pro, not the Mac Studio.** The Studio serves this fleet's
production traffic and holds 90 GB of resident models; a pull-and-load
experiment there is exactly the shape of the July panic.

**Done when:** each of our two MLX models is classified *runnable on Ollama-MLX*,
*runnable only on llama.cpp*, or *not importable* — with the `/api/ps` `runner`
value recorded for each.

## Phase 3 — Test the feature gap the binary already suggests

Only for models Phase 2 classified as Ollama-MLX-runnable.

- **KV quantization.** The compactor depends on `--kv-bits 8`. Confirm whether
  Ollama's MLX runner exposes any equivalent. The `strings` evidence says no;
  confirm it properly before relying on it, because absence of a string is not
  absence of a feature.
- **Speculative decoding.** `draft-model` / `MTP` strings exist. Is it
  configurable per model, or MTP-only for models that ship an MTP head? Our
  compactor pairs Qwen3-Coder-30B with a Qwen3-1.7B draft and measures ~94 tok/s
  with it.
- **Concurrency.** Ollama's MLX runner decodes **one request at a time**
  (`mlxrunner/runner.go` is a single loop), and `decode_parallelism_for` already
  mirrors that per model. For the compactor this is free — 3% overlap, measured.
  For GLM-4.7-Flash serving interactive traffic it is not, and that asymmetry
  may split the decision per model rather than per subsystem.

**Done when:** each gap is confirmed-present, confirmed-absent, or
irrelevant-to-our-use, with the evidence recorded.

## Phase 4 — Decide, per model, and write it down

The decision space, which Phase 2–3 narrow rather than this plan choosing in
advance:

| outcome | consequence |
|---|---|
| Ollama-MLX runs both models with acceptable gaps | Single-host `mlx_lm.server` becomes removable; **keep `mlx.launch` distributed** |
| Runs one (likely GLM, not the compactor, if the KV gap is real) | Subsystem shrinks but stays; the `mlx:` prefix keeps earning its place |
| Runs neither | Keep as-is, and record why so this is not re-opened for another five versions |

Whatever the outcome, two things are true and belong in the write-up: **routing
remains the herd's value** — a faster Ollama improves the herd for free — and
**distributed multi-Mac stays ours** regardless.

**Done when:** `docs/issues.md`'s MLX-runner entry is resolved with a per-model
verdict, and CLAUDE.md's `mlx:` gotcha states the current premise rather than the
2026-07-17 one. The gotcha already carries a *"not re-measured since, and 0.40
made MLX the default runner, so treat the comparison as stale rather than
settled"* marker added 2026-10-11; this plan is what removes that marker.

---

## Order and dependencies

Phase 1 is independent and useful on its own. Phase 2 gates Phase 3 (no point
testing features of a runner that will not select). Phase 3 gates Phase 4. Phase
1 ships with the duplicate-runner plan; Phases 2–4 need an idle machine and are
the slow part.

## Open questions

1. **Does `ollama create` accept MLX-community safetensors directly**, or does it
   need a conversion step that changes the weights we have measured?
2. **Is the Ollama version pinnable?** This subsystem's premise went stale
   because Ollama auto-updated across five minors unrequested. The July plan
   raised this and it is still open — and 0.40 arriving by reboot is the third
   instance.
3. **Does the MacBook Pro have the memory headroom** for Phase 2 at all (128 GB,
   and the compactor model is ~17 GB resident)?

## What this plan is not

It is not a deprecation plan, and it does not assume the answer is "remove the
subsystem". July's evidence pointed that way for single-host use and then Q4
showed the question was premature; the honest position is that the premise is
*unverified*, in both directions, and that a 2,881-line subsystem with 203 tests
deserves a measurement before either keeping or cutting it.
