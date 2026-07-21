---
title: "Extraction and the gate"
description: "The relevance gate, the two extraction backends, and the one combined call."
---

This page starts from a pending chunk, which [Chunking and embedding](/docs/dev/write/chunking/)
produced, and stops at a proposed `Extraction` of entities and dated facts.
[Grounding and consolidation](/docs/dev/write/consolidation/) covers that proposal next. Both stages
obey the live ontology, which [Entities, facts, ontology](/docs/user/concepts/graph/) sketches.

## The cheap exits come first

`extract_and_consolidate` in `src/aizk/graph/build.py` orders the work so the expensive model call
is the last thing anyone reaches.

```text
pending chunk
    -> source declarations and journal lines ----------------------+
                                                                    |
    short and nothing declared? --- yes --> mark processed, done    |
       | no                                                         |
       v                                                            v
    AIZK_EXTRACT_BACKEND                                        Extraction
       |- gliner --> one sidecar graph pass -------------------->   ^
       |- llm ----> GLiNER2 gate (ms on GPU)                        |
                       |- below the floor --> empty extraction -->  |
                       |- relevant -------> one LLM call / 2048 --> +
```

The first exit is the short chunk. `extract_min_chars` defaults to 80, and a shorter chunk is marked
processed and skipped, but only when the deterministic pass declared nothing. A two-line note
carrying a `Type` declaration or a dated journal entry still gets written, because that content came
from the author rather than a model.

## Explicit declarations

`SourceDeclaration.from_text` in `src/aizk/extract/declaration.py` reads a compact prelude a
self-describing Markdown note may carry. `_declaration_block` stops at the first line after the
title that is not a `Type` line, a relation line or a tag, since converted documents are full of
ordinary bullets beginning with the word Type.

Three forms are recognized. `- Type Person` sets the subject type. `#project: Ledger` is a tag that
becomes a `related_to` edge to that typed entity, and a tag naming the title declares the title's
own kind. `- works on [Project] Ledger` becomes a real predicate. Every declared kind and predicate
resolves against the live ontology through `canonical`, so an unknown spelling fails loudly rather
than inventing a type.

`journal_facts` separately parses `- YYYY-MM-DD: statement` lines into `observes` facts dated by the
line itself. Declarations are read only from the first chunk, since `source_extraction` guards them
with `chunk.ord != 0`, while journal lines are read from every chunk. These facts carry their own
source line as the quote and go straight into the write path, skipping the grounding audit, since
the author wrote them literally and there is no model output to verify.

## The gate

`GateClient.relevant` in `src/aizk/serving/gate/client.py` sends the chunk to a GLiNER2 sidecar's
`/classify` route as one multi-label task over `Ontology.current().gate_labels`, every extractable
entity kind except the generic `Concept` fallback. Labels above `gliner_gate_threshold`, default
0.7, come back as a set.

The verdict is `bool(present - gate_floor)`. `gliner_gate_floor` defaults to `frozenset({"Person"})`,
so a chunk whose only signal is a person mention does not clear the gate. Almost every sentence names
somebody, so that alone is no claim worth a call.

The sidecar owns the weights, so aizk never loads a model in its own process. The gate and the
GLiNER extractor share one HTTP client and one throttle sized by `gliner_concurrency`, default 8,
with a `gliner_timeout` of 30 seconds, and a missing sidecar fails the job so PgQueuer can retain it
for diagnosis.

## Two backends, and why only one needs the gate

`Extractor.configured` picks the backend from `AIZK_EXTRACT_BACKEND`, a `Literal["gliner", "llm"]`
defaulting to `llm`. The gate is not a global switch but a property of the backend,
`Extractor.requires_gate`, which the base class answers `False` and `LLMExtractor` overrides to
`True`.

That asymmetry is economics, not quality. The LLM path costs a constrained generation of up to
`llm_extract_max_tokens`, default 2048, so a millisecond-scale encoder pass to skip it is obviously
worth it. The GLiNER path is already one encoder pass, so gating it would run that class of model
twice to save running it once. The GLiNER backend therefore extracts directly and lets its own
thresholds discard weak output.

## The one combined call

`LLMExtractor.extract` returns entities, facts and per-fact dates in one structured response. The
system prompt is the ontology prompt plus `extract_system_prompt`, and the user message wraps the
chunk in `<document>` tags.

:::caution[A chunk is data, not instructions]
The prompt states plainly that the `<document>` contents are data and never instructions, which
stops a hostile note from steering extraction. Keep that guarantee if you touch the prompt.
:::

Windowing reuses the ingestion splitter, `chunk_text(text, settings.extract_window_size)` with
`extract_window_size` defaulting to 2048. Since stored chunks are also 2048 characters, an ordinary
chunk is one window and one call.

`_extract_bounded` handles a window that still overflows the model's context. It catches
`ModelHTTPError`, and `_context_overflow` accepts it only when the status is 400 and the message
contains `maximum context length`, so an ordinary bad request is never swallowed. On a real overflow
it re-chunks at half length and recurses, re-raising if the text will not split into two spans or is
shorter than two characters.

## The wire contract

`WireExtraction` in `src/aizk/ontology/wire.py` is the strict schema the response is validated
against, every field capped so a runaway generation cannot exhaust the token budget before the
required fields.

| Field | Meaning | Cap |
|---|---|---|
| `e` | entities | 16 per window |
| `f` | facts | 8 per window |
| `e[].n` | entity name, a plain noun phrase | 160 chars |
| `e[].t` | entity type | 64 chars |
| `e[].suggested_type` | a more specific type when `t` fell back to `Concept` | 96 chars |
| `f[].s`, `f[].o` | subject and object names | 160 chars each |
| `f[].p` | predicate | 64 chars |
| `f[].statement` | self-contained sentence | 384 chars |
| `f[].quote` | one contiguous supporting substring | 1 to 256 chars |
| `f[].date` | the fact's own date, when the text gives one | 64 chars |
| `f[].k` | epistemic kind, default `world` | enum |

The keys are terse because grammar-constrained tokens cost, while the descriptions carry the meaning
the model reads. `quote` has a minimum length of one, so the schema itself refuses a fact with no
evidence, and the prompt forbids ellipses and joined passages because the next stage checks the
quote against the source character for character.

A `suggested_type` keeps the closed vocabulary open. When the model falls back to `Concept` it may
name something more specific, and `prepare_entities` resolves those against the catalog in one
embedded lookup before the entity is written.

The GLiNER backend produces the same `Extraction` from grounded spans. It keeps the top 8 relations
by the weaker endpoint confidence, caps entities at 16, drops self-relations, and builds each
statement and quote from `_excerpt`, the smallest sentence-like span covering both ends.

## Next

<div class="not-content">

- [Grounding and consolidation](/docs/dev/write/consolidation/) covers what survives the audit.
- [Chunking and embedding](/docs/dev/write/chunking/) covers where these chunks came from.
- [Graph tables](/docs/dev/store/graph-tables/) has the rows extraction eventually writes.
- [Extraction and models](/docs/dev/eval/extraction/) has the measurements behind the defaults.

</div>
