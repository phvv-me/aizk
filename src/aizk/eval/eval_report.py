from patos import FrozenModel


class EvalReport(FrozenModel):
    """The aggregate quality of recall on our own corpus, the harness's honest scorecard.

    n: number of evaluation items scored.
    hit_at_k: fraction of items whose expected fact appeared in recall under the current settings.
    ndcg_at_k: ranx ndcg@k under the current settings, rewarding the expected fact ranking high.
    mrr: ranx mean reciprocal rank of the expected fact under the current settings.
    mean_judge: mean answerability the LLM judge gave, null when judging is off.
    per_config: hit-at-k keyed by the rerank and ppr (multi-hop personalized-pagerank) toggle the
        recall was scored under.
    comparison: ranx.compare significance table across the toggles, null when there is no gold.
    significant_best: the toggle label that significantly beats the current config on ndcg, the
        self-improve flip signal, null when no sweep clears the significance threshold over it.
    fixed_hit_at_k: hit-at-k of the fixed retrieval mix in the routed-versus-fixed A/B, null when
        there is no gold to score the A/B over.
    routed_hit_at_k: hit-at-k of the query-routed retrieval mix in the same A/B, null without gold.
    routing_winner: `routed` when query routing significantly beats the fixed mix on ndcg, else
        null, the data-driven signal on whether to turn query_routing on rather than faith.
    """

    n: int
    hit_at_k: float
    ndcg_at_k: float
    mrr: float
    mean_judge: float | None
    per_config: dict[str, float]
    comparison: str | None
    significant_best: str | None
    fixed_hit_at_k: float | None = None
    routed_hit_at_k: float | None = None
    routing_winner: str | None = None
