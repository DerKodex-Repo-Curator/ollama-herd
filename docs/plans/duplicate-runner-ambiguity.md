# One model, two runners — removing the ambiguity the `num_ctx` pin depends on

**Status**: ✅ **COMPLETE (2026-10-11).** All four phases implemented and
verified live. Filed as
[`docs/issues.md` → "One model resident twice makes 'the resident context'
ambiguous, and the pin guesses"](../issues.md) on 2026-10-10, after observing it
twice on the live fleet.

---

## Why this plan exists

An Ollama upgrade can rewrite the manifest digest of a model that is **already
resident**. `0.40.2` did exactly that to both of this fleet's models within a
minute of each other:

```
gemma3:27b     a418f5838eaf -> 8dda00f4f636   2026-10-10T20:21:09
gpt-oss:120b   a951a23b46a1 -> ad84bf7720de   2026-10-10T20:22:01
```

A runner loaded before its rewrite keeps the old digest. The next request for
that name resolves to the new one, which Ollama treats as a different model — so
it starts a **second runner for the same name**, and with
`OLLAMA_KEEP_ALIVE=-1` neither copy ever idles out. Resident memory went
114 GB → 156 GB for two logical models until a human looked.

The wasted memory is the visible half. The half that matters is that
**`LoadedModel` carries no digest**, so herd cannot distinguish the two runners
even in principle, and the ~15 call sites that resolve a model *name* against
`models_loaded` disagree about what to do when there are two:

| call site | behaviour on a duplicate |
|---|---|
| `streaming._get_loaded_context` | **first match wins** — this is the `num_ctx` pin's input |
| `streaming._find_context_upgrade` | first match (`break`) for `current_size` |
| `queue_manager` model size | first match (`break`) |
| `context_optimizer` allocated ctx | `max()` |
| `health_engine` `num_ctx_unmanaged` | `max()` |
| `health_engine` `num_ctx_override_inert` | one row **per entry** — names the same model twice |
| `health_engine` `kv_cache_bloat` | `sum(size_gb)` — counts one model twice |
| `health_engine` `underutilized_memory` | `len(models_loaded)` — 3 for 2 models |
| `serializers` `models_loaded_count` / `free_slots` | a duplicate consumes an apparent slot |
| anything building `{m.name for m in …}` | collapses harmlessly |

Both live duplicates happened to agree on context, so the pin's first-match
answer was **right by luck**. Simulated with them differing — which a manifest
rewrite plus one unmanaged cold load produces — the pin sends `131072` for a name
whose other runner is at `32768`. Which runner serves a request is Ollama's
choice, so that forces the unload/reload the pin exists to prevent,
intermittently, while appearing to work most of the time.

**This is a gap the 2026-10-09 pin created the conditions to notice, not one it
introduced.** The ambiguity predates it; the pin is simply the first consumer
whose correctness depends on the answer being unique.

## What the audit changed

Two findings cut the work substantially.

**The data is already on the wire, at one site.** `/api/ps` returns `digest`
today — confirmed live, keys are `['context_length', 'details', 'digest',
'expires_at', 'model', 'name', 'runner', 'size', 'size_vram']` — and
`LoadedModel` is constructed in exactly **one** place,
`common/ollama_client.py:50`. Step 1 is two fields and one edit, not a plumbing
exercise.

**`runner` arrives free in the same edit**, and it is
[`ollama-mlx-runner-subsystem-decision.md`](ollama-mlx-runner-subsystem-decision.md)'s
Phase 2 verbatim. **Ship it once, for both plans.** Note that plan's audit found
`ModelTagMeta.format` already predicts Ollama's runner choice for every
*available* model (`format == "safetensors"` is `IsMLX()`), so `runner` on
`LoadedModel` confirms what was actually chosen for *loaded* ones — the two are
complementary, not duplicates.

**There is already a shared-definition precedent to follow rather than invent.**
`serializers.model_resident_on_node` was moved there precisely so the scorer and
the preloader could not drift on "is this model resident"; its docstring says so.
The new helper belongs beside it, for the same reason, and the three `num_ctx`
checks already share `_operator_num_ctx_overrides()` on the same principle.

## Deliberately excluded

- **Fixing Ollama's `ollama stop` behaviour.** `stop <name>` removes the
  *current* digest and leaves the stale one — measured on both models, so it
  takes two calls. That is upstream behaviour; we document it and move on.
- **Preventing the duplicate.** herd does not control when Ollama rewrites a
  manifest. The goal is to be correct and visible in its presence, not to stop it.
- **Deduplicating every count in one pass.** Step 3 fixes the counts that feed a
  health card or a client-visible field. `models_loaded_count` on a dashboard tile
  being briefly off by one is not worth coupling to this.

## Implementation principles

- **Refuse to guess.** The helper must have an "unknown" answer, and the pin must
  already handle it — `_pin_resident_num_ctx` returns 0 and passes through
  untouched when the resident context is unknown. Ambiguity is the same case as
  absence; it must not become a third code path.
- **One definition, imported by every caller.** Not a second helper beside the
  first, which is the drift this exists to prevent.
- **No new env flag.** Correct behaviour is the behaviour.

---

## Phase 1 — Carry the digest (and the runner) — ✅ DONE 2026-10-11

Add `digest: str = ""` and `runner: str = ""` to `LoadedModel`; populate both at
`common/ollama_client.py:50` from the `/api/ps` fields. Defaults empty so a
mixed-version fleet and older agents keep working unchanged, matching how
`models_available_sizes` and `max_loaded_models` were introduced.

**Done 2026-10-11.** `/fleet/status` carries both per loaded model; defaults are
`""` so a pre-0.40 Ollama and an older agent are unaffected. Six tests in
`tests/test_models/test_loaded_model_runner_digest.py`, including the real
captured 0.40.2 payload, an older-agent payload with neither key, and one
asserting `runner` is not conflated with herd's `mlx:` routing prefix. Phase 2
of the MLX plan is the same change — it shipped once.

## Phase 2 — One definition of the resident context, which can say "unknown" — ✅ DONE 2026-10-11

Add `resident_context_for(model, node)` to `server/serializers.py`, beside
`model_resident_on_node`:

- one runner for the name → its context
- several runners, **all agreeing** → that value (unambiguous)
- several runners **disagreeing** → `0`, meaning unknown
- no runner → `0`

Then route the first-match-wins callers through it: `_get_loaded_context`,
`_find_context_upgrade`'s size lookup, and the queue's model-size lookup. Leave
the `max()` callers (`context_optimizer`, `num_ctx_unmanaged`) explicitly alone —
they are asking "what is the largest window in play", which is a different and
already-correct question. Say so in the docstring, or someone will "unify" them.

**Done when:** a test builds a node with two runners for one name at different
contexts and asserts the pin sends **no** `num_ctx`, and another with two
agreeing runners asserts it sends the agreed value.

## Phase 3 — Stop double-counting — ✅ DONE 2026-10-11

Dedupe by digest (falling back to name when digest is empty) in:
`kv_cache_bloat`'s `total_vram_gb` / `total_expected_gb`,
`underutilized_memory`'s model count, and `serializers`'
`models_loaded_count` / `free_slots`. Also give
`num_ctx_override_inert` one row per `(model, node)` rather than per entry.

Note `kv_cache_bloat` escaped firing during the live incident only because the
per-entry ratio stayed under 1.5x; its *reported totals* were wrong both times.

**Done when:** a duplicate node fixture yields a model count of 2, not 3, and the
VRAM total counts each runner's memory once while attributing weights once per
logical model.

## Phase 4 — Make the duplicate itself visible — ✅ DONE 2026-10-11

A health check: one name, two digests, same node. Pure waste, never intentional,
and with `KEEP_ALIVE=-1` it cannot resolve on its own — 66 GB sat stranded here
until someone looked. The remediation is non-obvious and belongs in the card:

> `ollama stop <name>` **twice** — the first call takes the current digest and
> leaves the stale one — then one request through the router to cold-load with
> the override applied.

Apply the standing test for a new check, from six false positives in two weeks:
**what does it fire on when everything is working?** Answer: nothing, because two
digests for one name on one node is not a state Ollama reaches in normal
operation. And **is it deriving something it could be told?** It is told — by
`digest`, from Phase 1.

**Done when:** the check fires on a two-digest fixture, stays silent on a
single-runner and an empty-digest (older-agent) fixture, and its severity
reflects reclaimable GB rather than the mere fact of duplication.

---

## Order and dependencies

Phase 1 gates everything and is shared with the MLX plan. Phase 2 is the
correctness fix and is the reason this plan exists. Phase 3 and Phase 4 are
independent of each other and both depend only on Phase 1 — Phase 4 is the one
that would have caught the incident in minutes rather than hours.

## Open questions

1. ~~**Is `digest` stable across an Ollama restart** for an unchanged model?~~
   **ANSWERED 2026-10-11 — yes, stable.** A full Ollama restart (quit the
   Electron parent, reap `llama-server`, relaunch) left both models on their
   same digests: `gpt-oss:120b` `ad84bf7720de` and `gemma3:27b` `8dda00f4f636`
   before and after. So dedupe-by-digest is sound, which Phase 3 relies on.
2. **Should the pin prefer the runner that is actually serving?** `/api/ps` gives
   no such field, and `requests_active` is per entry — worth checking whether it
   discriminates in practice, though "refuse to guess" is correct regardless.
3. **Does the same ambiguity reach `mlx_servers`?** They are keyed by model and
   port, so two entries for one model are possible by configuration. Out of scope
   here, but worth a look while the helper is being written.

## What this plan is not

It is not a fix for Ollama's manifest rewriting or its `stop` semantics, and it
does not try to keep duplicates from happening. It makes herd correct in their
presence and loud about them — which is what the incident showed was missing.


---

## Outcome (2026-10-11)

**Phase 2** — `serializers.resident_context_for(model, node)` is the one
definition, beside `model_resident_on_node` for the same reason. It returns 0
("unknown") when a name's runners disagree on context, which is deliberately the
same value as "not resident" so `_pin_resident_num_ctx` passes the request
through untouched rather than asserting a window — ambiguity did not become a
third code path. `streaming._get_loaded_context` delegates to it. The other two
first-match sites (`_find_context_upgrade`'s size, the queue's size) now take
the **largest** entry rather than the first, because both gate memory decisions
where understating resident size is the unsafe direction. The `max()` callers
were left alone, as planned.

**Phase 3** — `distinct_resident_models` (digest-keyed) and
`distinct_resident_model_names`. VRAM totals count every *runner*, since two
runners are two processes holding real memory; `models_loaded_count`,
`free_slots` and `underutilized_memory` count *logical models*, which is what
they mean. `num_ctx_override_inert` emits one row per `(model, node)`.

**Phase 4** — the `duplicate_runners` check (49 total). Severity follows
reclaimable memory: the gemma3 duplicate (23.4 GB) is INFO, the gpt-oss one
(66.4 GB) is WARNING. It calls out divergent contexts explicitly, because that
is when Phase 2 stops injecting `num_ctx` for the model. It needs two
*distinct* digests, so an older agent reporting none is never flagged — two
entries we cannot tell apart are not evidence of two runners.

**Verified live:** the pin fires off the new helper, `models_loaded_count` is 3
for three distinct models with `free_slots` 7, and `duplicate_runners` is
correctly silent with no duplicates present. 1886 tests.

One detail worth keeping: `distinct_resident_models` reads the digest with
`getattr(loaded, "digest", "")`, not attribute access. This module is handed
duck-typed node objects throughout, and plain access broke 15 existing tests
immediately — the codebase's own idiom (`decode_parallelism_for`,
`model_resident_on_node`) was already `getattr` with a default for exactly that
reason.
