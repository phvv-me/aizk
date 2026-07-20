---
title: "Benchmarks and evaluation"
description: "Three separate evaluation levels, from unit tests through production retrieval to external benchmarks."
---

Aizk keeps three evaluation levels separate. A test proves a contract. The production retrieval
benchmark measures memory already stored in one deployment. An external benchmark imports its own
history into an isolated scope before it asks any question.

## Internal evaluation

`chefe run aizk-eval bench` samples visible local facts, global summaries, and two-hop fact paths from the
stored corpus. The LLM turns each source into a probe, and the benchmark scores the maximal plan that
production recall always uses. It reports hit rate, nDCG, MRR, optional answerability judging, and
median latency for each stratum. This is useful for regression checks on a real corpus. It is not
an external benchmark score. Pass `--user` when the corpus lives outside the default system scope.

`chefe run aizk-eval plans` is the diagnostic study. It compares the production plan with retired forced
plans and the retired router without changing production behavior. It can also replay graph
seeding variants and the extraction gate. `chefe run aizk-eval scale` grows an isolated synthetic corpus and
records the point where latency or storage crosses its declared budget.

`chefe run aizk-eval trace` shows statement rank, cross-encoder score, final merit rank, and the packing cut
without updating access history. `chefe run aizk-eval management` discovers every visible Area and Project
brief and runs twenty grounded questions for each one. The strict score requires the subject's own
current brief to rank first, while hit rate still records whether it survived anywhere in the
packed context. The report includes MRR plus end-to-end p50 and p95 latency. This makes the current
brief itself the retrieval reference and exposes incidental evidence that would otherwise hide a
correct answer below the first result.

The final 2026-07-15 production management cell covered all eight Areas and all forty Projects,
including paused, completed, cancelled, and archived work. It ran 960 questions with four
concurrent recalls, an eight-candidate target, and the production 2,048-token budget. The intended
current brief was present and first for 960 of 960 questions, every subject had MRR 1.0, p50 was
1,778.5 ms, and p95 was 2,103.1 ms. The first run reached 960 hits but only 945 first-place results.
Every miss came from one managed title being contained inside another, such as `JLPT N2` inside
`JLPT N2 Window Weekly Plan`. Giving direct identity authority only to the maximal overlapping
title fixed all fifteen cases without changing the maximal retrieval plan. This cell proves source
identity and packing correctness. It does not by itself judge whether an answer model used every
field in the brief correctly.

Seven selected briefs from the vendored memory papers formed a second production cell. One focused
question per paper ranked its own brief first for all seven papers. The set covered GroupMemBench,
Does Memory Need Graphs, Hindsight, APEX-MEM, LongMemEval-V2, Memora, and Mem2ActBench. Sequential
representative recalls later took about 1.0 to 2.3 seconds. The durable design conclusions were
also written into the Zettelkasten, including the need to preserve speaker semantics, keep raw
evidence beside graph projections, and score obsolete contamination and downstream use.

The 2026-07-12 query regression used PostgreSQL 18, VectorChord 1.1.1, and 100,000 chunks plus
100,000 live facts. Moving chunk reads through document RLS and starting dense facts from bounded
content candidates reduced local database execution from about 855 ms to 340 ms. The same pass
removed temporary I/O and reduced the cold multihop plan from about 3.02 seconds to 2.05 seconds.
Warm multihop execution was about 0.5 seconds. A focused Vault check over 23 related notes and ten
explicit source qrels kept identical rankings with hit at 8 and MRR both equal to 1.0. This is a
regression cell, not a published quality score.

The 2026-07-12 ranking upgrade was validated on a planted corpus of 128 topics, 99,840 chunks,
and 104,448 live facts whose ground truth encodes each mechanism: dense-invisible chain facts
reachable only through the graph, high-degree hub entities, fresh and stale claim twins at equal
cosine distance, near-duplicate mega documents, and off-corpus noise questions. Inside the final
packed context, mention-seeded personalized PageRank recovered 508 of 512 planted chain facts
where the previous recursive walk recovered 128, access-decay blending ranked the fresh twin
first in 128 of 128 pairs where distance-only ordering managed 13 of 32 at the smaller scale, the
per-document cap lifted distinct sources from 1 to 4 of 8, and the calibrated relevance floor
packed zero lines across every noise question. Local recall executed at about 259 ms p50 and
multihop at about 465 ms p50 on this cell, with the packing walk's ordered candidates now
materialized so the recursive budget walk no longer re-runs every lane per kept row. The floor
default of 0.65 comes from real Qwen3-VL embeddings, where relevant vault chunks landed at
cosine distance 0.27 to 0.49 and off-corpus questions bottomed out at 0.60 to 0.75. This is a
regression cell, not a published quality score.

The 2026-07-12 embedder shootout chunked the real vault with the production chunker into
1,903 spans over 1,156 notes and scored self-retrieval for 1,101 title queries per candidate
model at the schema's 1,024 dimensions. `Qwen3-VL-Embedding-2B`, the multimodal default,
reached hit@5 88.0% and MRR 0.794; `Qwen3-Embedding-0.6B` 89.3% and 0.792;
`Qwen3-Embedding-4B` 90.1% and 0.802, rising to 90.3% and 0.807 at its native 2,560
dimensions. Native dimensions added under one point everywhere, validating the Matryoshka
truncation. The text-only models push the nearest off-corpus distance from 0.46 to 0.63,
a cleaner abstention margin, at the cost of the image lane. Absolute cosine values are
model-specific geometry under instruction-asymmetric embedding and only margins carry
meaning, so cross-model distance comparisons say nothing about quality.

The 2026-07-12 reranker cell scored both original Qwen3 reranker checkpoints served through
vLLM's yes/no-classifier conversion, reranking the embedder's own top 8 for 253 real vault
queries. Without the official prompt scaffold both checkpoints ranked filler above answers
and collapsed MRR from 0.90 to about 0.40, so the scaffold is correctness, not style. With
it, the 0.6B still degraded MRR to 0.77 while the 4B held 0.91 against a 0.90 baseline with
little headroom. The lane therefore ships with the 4B checkpoint, stays off without a
configured endpoint, and owes its real quality verdict to a benchmark with genuine question
headroom rather than title self-retrieval.

The 2026-07-14 crimson smoke cell exercised the deployed stack with three vendored papers, one
repository guide, and five source files. Extraction completed all 220 chunks and produced 2,086
entities, 2,033 facts, and 2,079 profiles. A manual analysis of eight cross-document questions found
three strong answers, two partial answers, and three justified abstentions. The HAWQ-V3 and gauge
theory questions recovered the core mechanisms. LongMemEval-V2 definition and comparison questions
were weaker. Two abstentions followed incomplete retrieval, while one comparative abstention had
enough source evidence and exposed an overly conservative reader. Median recall took 6.81 seconds
and median answer generation took 1.42 seconds on the two RTX 3090 host. This is a real-stack
diagnostic over a small corpus, not an external benchmark score.

The first 2026-07-14 extractor smoke cell compared the selectable LLM and GLiNER backends on four
recent dense research chunks from the live Crimson database. GLiNER ran in 10.9 to 15.5 seconds on
CPU, while the LLM took 20.1 to 60.1 seconds on its GPU. The LLM produced 31 facts across the four
chunks. GLiNER base produced eight and emitted none on two chunks. Exact triple agreement was zero,
and manual inspection found several GLiNER relations that did not express the source meaning.

A second cell moved GLiNER to the same GPU stack, fixed its missing long-text integration, and
compared base, large, and the LLM on the latest four dense vault chunks. Base at a 0.5 threshold
used 1.5 GB of VRAM and returned 11 relations in 2.76 seconds including first-request warmup. Large
used 2.4 GB and returned 13 in 2.84 seconds. Large removed some obvious errors, but both checkpoints
still emitted self-relations and predicates that contradicted the source. At 0.6, large returned six
relations with one empty chunk and retained several wrong predicates. At 0.7, it returned two
plausible relations with two empty chunks in 0.27 seconds after warmup. The LLM returned 32 much
more coherent facts with no empty chunk in 75.51 seconds. Its strict quote substring check passed
for every fact in three chunks and had at least one mismatch in the fourth.

The result keeps the LLM as the production graph writer. GLiNER2 large on GPU is the shared cheap
gate and remains a selectable experimental writer at the safer 0.7 threshold. The large model is
nearly free beside the existing lanes, but speed cannot compensate for wrong graph edges. These
cells are small diagnostics rather than published quality benchmarks.

The July 17 production check used Gemma 4 31B on GPU 1 against five stored documents about
frontend architecture, authentication, hashing, artifacts, and the public memory interface. The
final contract produced twenty proposed facts and accepted all twenty through deterministic source
grounding. Earlier cells exposed three independent interface failures. A nullable wire quote made
the model omit evidence, legal JSON whitespace exhausted a bounded response, and the phrase
shortest quote encouraged ellipses between separate source spans. Requiring a quote, enabling
compact XGrammar output, and requiring one contiguous character-for-character substring fixed
those failures. Grounding also ignores Markdown backticks while rejecting actual word changes.

The same host then tried Gemma 4 E2B with an 8,192 token context and 95 percent GPU allocation. It
used about 9.5 GB of the 24 GB card and did not become healthy within two bounded five-minute
windows. The service was stopped as stalled and production returned to 31B. This operational
failure does not erase the earlier E2B quality measurements, but it confirms that E2B neither
meets the dedicated-card requirement nor provides a safer production path.

The same database held 10,000 entities and 100,000 live facts for the graph write-path check.
Loading every full live fact for 32 subjects and reranking in Python took about 833 ms and spilled
1,455 temporary blocks. A typed lateral query ranked each candidate through the subject index,
selected only claim ID, predicate, object, statement, and raw cosine distance, and returned the top
five in about 39 ms without temporary I/O. Entity resolution now sends a whole extracted batch
through deterministic normalized IDs and bulk inserts. It never merges by semantic vector
proximity because related areas such as Health and Business are distinct graph nodes. Fact endpoint
lookup uses that same normalized identity, so capitalization and display formatting cannot turn a
grounded endpoint into a missing entity. Unchanged documents are removed before any embedding
request.

## GroupMemBench

The GroupMemBench adapter reads the released conversation and question schemas independently. It
does not copy the upstream implementation, whose repository currently declares no license. Each
domain gets one deterministic shared scope. The complete corpus contains 30,000 messages in each
domain. Every message keeps its author, role, channel, reply, phase, topic, decision marker, and
source time. Every question recalls as its named asking user.

```sh
chefe run aizk-eval groupmem /path/to/GroupMemBench --domain Finance --question-limit 2
```

The runner performs the complete path. It batches message embeddings, stores distinct message
identities, builds the graph, assembles asker-aware context, generates an answer from that context,
and judges it against the gold answer. Pydantic Evals owns the typed cases, concurrent execution,
LLM verdicts, durations, and failures. Question families remain separate in the report so an
overall score cannot hide failures on updates, time, speaker perspective, terminology, or
abstention.

The released comparison protocol uses `k=10`, GPT-5 for answer generation, and GPT-5 for judging.
Finance and Technology use the released solvability-filtered questions. Healthcare and
Manufacturing remain useful diagnostic domains, but their released questions are unfiltered.

A publishable report therefore requires the complete 30,000-message corpus, every question in one
filtered domain, the reference models, `k=10`, and no operational failures. A message or question
limit always marks the report diagnostic. Reusing the local LLM also marks it
diagnostic. The report records both model names and never turns a network, generation, database,
or evaluator failure into an ordinary wrong answer.

No Aizk GroupMemBench score is published yet. The adapter is implemented and tested, but a full
run requires the deployed embedding and extraction lanes and must not be replaced by a synthetic
estimate. The paper reports that its best evaluated system reaches 46 percent overall, which is a
useful difficulty reference rather than an Aizk result.

## Forgetting-aware scoring

`FAMAScore` implements the Memora paper equation per question.

```text
max(0, MPA - weight * (1 - FAA))
```

MPA is current-memory presence accuracy. FAA is obsolete-memory absence accuracy. The weight is
the number of forgetting criteria divided by all criteria. A relevant answer that also repeats an
invalidated memory therefore loses credit.

## Verification posture

The suite uses Hypothesis for algebraic and authorization properties and parametrized tests for
backend matrices. Its migration test creates a disposable PostgreSQL database and upgrades it to
head, the single `0001_init` revision that builds the entire schema in one step. It inserts a real
scoped `document` and child `chunk`, including the original UUIDv8 content hash and source text.

The test then proves that the document title and content hash are stored unchanged, the artifact
links remain null for a plain text source, and the database reached the exact `0001_init` head. It
also verifies that `artifact`, `artifact_content`, `blob`, and `usage_event` were installed with
forced row security. Finally, it inspects the new chunk insert policy and confirms that a child
write must match both its parent document ID and exact scope set. The disposable database is
dropped even after a failure.

Alembic autogenerate separately returns an empty revision against the current models. The RLS
verifier checks every scoped table and policy in the PostgreSQL catalog.

Sources include [GroupMemBench](https://arxiv.org/abs/2605.14498),
[Memora](https://arxiv.org/abs/2604.20006), and
[LongMemEval-V2](https://arxiv.org/abs/2605.12493). Query profiling follows
[PostgreSQL 18 EXPLAIN](https://www.postgresql.org/docs/18/sql-explain.html), and filtered vector
search follows [VectorChord prefilter](https://docs.vectorchord.ai/vectorchord/usage/prefilter.html).
