# Benchmarks and evaluation

Aizk keeps three evaluation levels separate. A test proves a contract. The internal harness
measures retrieval over memory already stored in one deployment. An external benchmark imports
its own history into an isolated scope before it asks any question.

## Internal evaluation

`aizk eval bench` samples visible live facts or reads one question per line from a file. It reports
hit rate, nDCG, MRR, optional answerability judging, and the multihop expansion breakdown.
This is useful for regression checks on a real corpus. It is not an external benchmark score.

`aizk eval sweep` measures quality, latency, host memory, GPU memory, and vector storage across a
bounded configuration matrix. `aizk eval scale` grows an isolated synthetic corpus and records the
point where latency or storage crosses its declared budget.

The 2026-07-12 query regression used PostgreSQL 18, VectorChord 1.1.1, and 100,000 chunks plus
100,000 live facts. Moving chunk reads through document RLS and starting dense facts from bounded
content candidates reduced local database execution from about 855 ms to 340 ms. The same pass
removed temporary I/O and reduced the cold multihop plan from about 3.02 seconds to 2.05 seconds.
Warm multihop execution was about 0.5 seconds. A curated Vault check over 23 related notes and ten
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

The same database held 10,000 entities and 100,000 live facts for the graph write-path check.
Loading every full live fact for 32 subjects and reranking in Python took about 833 ms and spilled
1,455 temporary blocks. A typed lateral query ranked each candidate through the subject index,
selected only claim ID, predicate, object, statement, and raw cosine distance, and returned the top
five in about 39 ms without temporary I/O. Entity resolution now sends a whole extracted batch
through one `VALUES` relation, and unchanged documents are removed before any embedding request.

## GroupMemBench

The GroupMemBench adapter reads the released conversation and question schemas independently. It
does not copy the upstream implementation, whose repository currently declares no license. Each
domain gets one deterministic shared scope. The complete corpus contains 30,000 messages in each
domain. Every message keeps its author, role, channel, reply, phase, topic, decision marker, and
source time. Every question recalls as its named asking user.

```sh
aizk eval groupmem /path/to/GroupMemBench --domain Finance --question-limit 2
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
limit always marks the report diagnostic. Reusing the local extraction model also marks it
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
backend matrices. A fresh `aizk_test` database migrates from `0001_init`, and Alembic autogenerate
returns an empty revision against the current models. The RLS verifier separately checks every
scoped table and policy in the PostgreSQL catalog.

Sources include [GroupMemBench](https://arxiv.org/abs/2605.14498),
[Memora](https://arxiv.org/abs/2604.20006), and
[LongMemEval-V2](https://arxiv.org/abs/2605.12493). Query profiling follows
[PostgreSQL 18 EXPLAIN](https://www.postgresql.org/docs/18/sql-explain.html), and filtered vector
search follows [VectorChord prefilter](https://docs.vectorchord.ai/vectorchord/usage/prefilter.html).
