---
title: "Fusion and reranking"
description: "Reciprocal rank fusion in SQL, then one cross-encoder ordering everything by merit."
---

Ranking happens twice. PostgreSQL fuses three chunk rankings into one score inside the recall
statement, and Python reorders the whole candidate list afterward with a cross encoder. This page
assumes you have read [the lanes](/docs/dev/read/lanes/), since the first half is what
`SourceLane` calls. The code is `src/aizk/store/models/tables/chunk.py` and
`src/aizk/retrieval/rerank/rescore.py`.

```text
  dense (distance under the floor) ─┐
  bm25  (tokenize, to_bm25query)   ─┼─▶ RRF, sum of 1 / (rrf_k + rank)
  title (named in query, longest)  ─┘             │
                                                  ▼
                        + promoted_bonus, + 1.0 when named
                                                  │
                                                  ▼
                 at most recall_per_document per document, then k
                                                  │
                                                  ▼
                  cross encoder scores the first rerank_depth
                                                  │
                                                  ▼
           direct and unshadowed first, then score, then evidence id
```

## Chunk.fused unions three rankings

Each ranking produces `(id, document_id, rank)` and each is cut at `fusion_depth` before anything
is joined, which keeps every one of them an index-friendly top-k scan.

**Dense** ranks `embedding @ qvec` ascending, guarded by `embedding IS NOT NULL`,
`distance < recall_max_distance` and `Document.is_active()`. The floor is what keeps an off-corpus
question from returning its least bad match.

**Lexical** is BM25 through VectorChord. The query goes through
`to_bm25query('ix_chunk_bm25', tokenize(:qtext, 'aizk_bm25'))` and ranks with the `<&>` operator
against a `bm25` column. That column and its index exist only in the migration and never on the
model, which is why the code reaches for them with `sqlalchemy.column("bm25")`. Scores come back
negative, so `raw_rank < 0` filters out the rows that matched nothing.

**Title** is the exact identity ranking. `Document.named_in_query()` lowercases both the title and
the `qtext` bind, replaces every non-alphanumeric run with a space, pads both with spaces and asks
whether the padded title occurs inside the padded query, requiring at least three characters. That
padding is what makes it a whole-token match rather than a substring accident. Its chunks rank by
`length(title) DESC` first, so the most specific named title wins, then by chunk `ord`.

The three are unioned and grouped, and each contributes `1 / (rrf_k + rank)` with `rrf_k`
defaulting to 60. A chunk found by two rankings collects both votes. Fusing positions rather than
scores is the point, since a cosine distance and a BM25 score are not comparable numbers.

:::note[Where this comes from]
Fusing ranks instead of raw scores is
[Reciprocal Rank Fusion](https://research.google/pubs/reciprocal-rank-fusion-outperforms-condorcet-and-individual-rank-learning-methods/).
The [references map](/docs/dev/prior-art/references/) traces every mechanism back to its source.
:::

## Chunk.hybrid scores, caps and cuts

`hybrid` joins the fused CTE to `document` and builds one score.

```python
source_score = (
    fused.c.rrf_score
    + case((promoted, bindparam("promoted_bonus", type_=Float)), else_=0.0)
    + case((Document.named_in_query(), literal(1.0)), else_=0.0)
)
```

`promoted_bonus` defaults to 0.01, which is a nudge, since a promoted document is evidence somebody
already found worth keeping. The named-title bonus is a hard-coded `1.0` and is not configurable,
which is deliberate because it is roughly two orders of magnitude larger than any RRF sum and so
functions as a class rather than as a weight.

A `row_number()` partitioned by `document_id` and ordered by score then enforces
`recall_per_document`, three by default, so one long document cannot fill the whole lane. The
survivors order by score and cut at `k`. The lane also projects `named_in_query()` as `direct`,
which is the only place that flag is set.

## merit_order reorders everything

`merit_order` takes the statement's rows in their lane-priority order and scores the first
`rerank_depth` of them, 50 by default, sending each candidate's rendered `line` to
`RerankClient`. Note that it scores against the raw `query`, not the speaker-rewritten string that
went to the embedder. The scores are zipped with `strict=True` and kept in a dict keyed by
`evidence_id`, which is exactly why `Candidate` carries that excluded field. `trace()` returns
those same scores.

`reordered` sorts the scored candidates by a three-part key.

```python
key = (-(candidate.direct and candidate.direct_title not in shadowed),
       -scores[candidate.evidence_id],
       candidate.evidence_id)
```

First comes the identity group, then merit inside it, then `evidence_id` so ties break exactly as
the statement ordered them. Candidates past the scoring depth are not sorted at all. They keep the
statement's order and are appended after the scored block, so the reranker changes the head of the
list and leaves the tail alone.

## Title shadowing

`_shadowed_titles` returns every named title that is strictly contained in another named title.

Without it, a question naming `JLPT N2 Window Weekly Plan` also directly names the document titled
`JLPT N2`, and both would land in the authoritative group. The broader document then competes on
equal footing with the one the question actually asked for. Shadowing drops the contained title out
of the identity group while leaving it in the ranking, so it can still win on merit. Two unrelated
titles named in the same question shadow neither, since neither contains the other, and they stay
peers.

The shadow test runs on `Candidate.direct_title`, which is the casefolded `source_title` and only
exists when `direct` is true.

## Next

<div class="not-content">

- [Budget packing](/docs/dev/read/packing/) turns this order into the response.
- [Retrieval tuning](/docs/dev/read/tuning/) has `rrf_k`, `fusion_depth` and `rerank_depth`.
- [Retrieval results](/docs/dev/eval/retrieval/) has what these choices measure.

</div>
