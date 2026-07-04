import contextlib
import math
import uuid
from collections.abc import Iterator

from loguru import logger
from patos import FrozenModel
from sqlalchemy import select

from ..config import settings
from ..extract.llm import structured
from ..retrieval import RecallResult, recall
from ..store import LiveFact, acting_as
from .eval_report import EvalReport
from .generated_question import GeneratedQuestion
from .judge_verdict import JudgeVerdict
from .qa import QA

# the rerank and ppr on-and-off matrix the harness sweeps so the report shows what each lever buys
# on our corpus, named so the per-config breakdown reads at a glance.
TOGGLES = ((False, False), (False, True), (True, False), (True, True))


@contextlib.contextmanager
def swept_settings(**fields: bool | int | float | str) -> Iterator[None]:
    """Temporarily overlay fields onto the live settings singleton, restoring them after the block.

    Mutates the shared `settings` object directly since `recall` and its lanes read config off it
    rather than as parameters. Only safe for the eval harness's own sequential sweep, never across
    a concurrent request.

    fields: `Settings` field names to overlay for the duration of the block.
    """
    previous = {key: getattr(settings, key) for key in fields}
    for key, value in fields.items():
        setattr(settings, key, value)
    try:
        yield
    finally:
        for key, value in previous.items():
            setattr(settings, key, value)


QUESTION_SYSTEM = (
    "Turn the given fact into one natural question a person would ask whose answer is that fact.\n"
    "Paraphrase it. Do not reuse the fact's own nouns, names, or key terms verbatim, reach for a\n"
    "synonym or a description instead, so answering the question needs the fact itself and not a\n"
    "surface word match. Write only the question, never referencing that you were given a fact."
)

JUDGE_SYSTEM = (
    "You judge whether a retrieved context answers a question. Read the question and the context\n"
    "and decide whether the context holds enough to answer it. Reply answerable true or false."
)


def significant_winner(data: dict, current: str, metric: str, max_p: float) -> str | None:
    """The swept config whose metric significantly beats the current, or null on a tie or noise.

    Only a config that both outscores the current on the metric and clears the significance
    threshold can win, so an automatic settings flip never happens on a noisy delta alone.

    data: a ranx compare Report's to_dict, carrying per-config scores and pairwise p-values.
    current: the live config's run label, the baseline every other config is tested against.
    metric: the ranx metric the significance decision reads, such as ndcg@k.
    max_p: the largest p-value a win may carry to count as significant rather than noise.
    """
    scores = {name: data[name]["scores"][metric] for name in data["model_names"]}
    winners = [
        name
        for name in data["model_names"]
        if name != current
        and scores[name] > scores[current]
        and not math.isnan(p := data[name]["comparisons"][current][metric])
        and p <= max_p
    ]
    return max(winners, key=lambda name: scores[name]) if winners else None


async def sample_facts(principal_id: uuid.UUID, n: int) -> list[str]:
    """Return up to n visible latest fact statements in a fixed order, the auto-eval source pool.

    principal_id: identity whose row level security visibility scopes the sample.
    n: maximum number of statements to sample.
    """
    async with acting_as(principal_id) as session:
        # `live_fact` already carries its own liveness gate, so this reads it directly rather than
        # opting the do_orm_execute listener out by hand the way a raw `FactClaim` read would.
        statements = await session.scalars(
            select(LiveFact.statement).order_by(LiveFact.id).limit(n)
        )
    return list(statements)


async def build_questions(
    questions: list[str] | None,
    principal_id: uuid.UUID,
) -> list[QA]:
    """Assemble the evaluation items, the caller's questions or ones synthesized from facts.

    With a caller question set each item carries no expected fact and the judge alone scores it.
    Without one it samples latest facts and asks the LLM to turn each into a question whose answer
    is that fact, so hit-at-k has a gold to check against.

    questions: the caller's questions, or null to synthesize them from sampled facts.
    principal_id: identity whose visibility scopes the sampled facts.
    """
    if questions is not None:
        return [QA(question=question, expected=None) for question in questions]
    pairs: list[QA] = []
    for statement in await sample_facts(principal_id, settings.eval_sample_questions):
        generated = await structured(QUESTION_SYSTEM, statement, GeneratedQuestion)
        pairs.append(QA(question=generated.question, expected=statement))
    return pairs


async def judge_answerable(question: str, context: str) -> bool:
    """Ask the LLM whether a recalled context answers a question.

    question: the evaluation question.
    context: the rendered recall bundle to judge.
    """
    user = f"Question.\n{question}\n\nContext.\n{context}"
    verdict = await structured(JUDGE_SYSTEM, user, JudgeVerdict)
    return verdict.answerable


def render_context(result: RecallResult) -> str:
    """Flatten a recall's facts and hits into the plain text the judge reads as its context.

    result: the fused recall bundle to flatten.
    """
    facts = [f"({fact.predicate}) {fact.statement}" for fact in result.facts]
    return "\n".join(facts + [hit.text for hit in result.hits])


def retrieved_scores(qa: QA, result: RecallResult) -> dict[str, float]:
    """Score one recall's ranked docs for ranx, labeling the expected fact's first match `rel`.

    Facts rank ahead of chunk passages and each doc takes a score that descends with its rank so
    ranx reads the retrieval order back. The highest-ranked doc carrying the expected fact, an
    exact fact statement or a passage that contains it, gets the `rel` id the qrels mark relevant.

    qa: the evaluation item carrying the expected fact.
    result: the recall bundle whose facts and passages are ranked.
    """
    texts = [fact.statement for fact in result.facts] + [hit.text for hit in result.hits]
    scores: dict[str, float] = {}
    matched = False
    for rank, text in enumerate(texts):
        hit = qa.expected is not None and (text == qa.expected or qa.expected in text)
        relevant = hit and not matched
        matched = matched or hit
        scores["rel" if relevant else f"d{rank}"] = float(len(texts) - rank)
    return scores


async def config_scores(
    gold: list[QA], principal_id: uuid.UUID, k: int
) -> dict[str, dict[str, float]]:
    """Recall every gold question under one config and score each ranking into a ranx run.

    gold: the evaluation items that carry an expected fact.
    principal_id: identity whose visibility scopes the recall.
    k: number of hits and seed facts each recall surfaces.
    """
    scores: dict[str, dict[str, float]] = {}
    for index, qa in enumerate(gold):
        result = await recall(qa.question, principal_id=principal_id, k=k)
        scores[f"q{index}"] = retrieved_scores(qa, result)
    return scores


async def routing_ab(
    gold: list[QA],
    principal_id: uuid.UUID,
    k: int,
    metrics: list[str],
) -> tuple[float, float, str | None]:
    """A/B the query-routed retrieval mix against the fixed one and report whether routing wins.

    Scores every gold question once with query_routing off and once on against one shared qrels.
    The winner is `routed` only when it both outscores the fixed run on ndcg and clears the
    significance threshold, so a noisy delta never declares a win.

    gold: the evaluation items that carry an expected fact.
    principal_id: identity whose visibility scopes the recall.
    k: number of hits and seed facts each recall surfaces.
    metrics: the ranx metric list, hit-rate first and ndcg second as run_eval orders them.
    """
    from ranx import Qrels, Run, compare

    qrels = Qrels({f"q{index}": {"rel": 1} for index in range(len(gold))})
    runs: dict[str, Run] = {}
    for label, routing in (("fixed", False), ("routed", True)):
        with swept_settings(query_routing=routing):
            runs[label] = Run(await config_scores(gold, principal_id, k), name=label)
    report = compare(qrels, list(runs.values()), metrics=metrics)
    winner = significant_winner(report.to_dict(), "fixed", metrics[1], settings.self_improve_max_p)
    fixed_hit = float(report.results["fixed"][metrics[0]])
    routed_hit = float(report.results["routed"][metrics[0]])
    return fixed_hit, routed_hit, winner


class ToggleSweepResult(FrozenModel):
    """The rerank/ppr (multi-hop personalized-pagerank) toggle sweep's own scored outcome, before
    the judge or final assembly.

    per_config: hit-at-k keyed by the rerank/ppr toggle label.
    headline: the ranx metric values for the current live toggle combination.
    comparison: ranx.compare's significance table across the toggles.
    significant_best: the toggle label that significantly beats the current config on ndcg, null
        when none clears the significance threshold.
    """

    per_config: dict[str, float]
    headline: dict[str, float]
    comparison: str
    significant_best: str | None


async def sweep_toggles(
    gold: list[QA], principal_id: uuid.UUID, k: int, metrics: list[str], current: str
) -> ToggleSweepResult:
    """Score every rerank/ppr toggle combination against one shared qrels, flagging the best.

    gold: the evaluation items that carry an expected fact.
    principal_id: identity whose visibility scopes the recall.
    k: number of hits and seed facts each recall surfaces.
    metrics: the ranx metric list, hit-rate first and ndcg second.
    current: the live config's run label, the baseline every toggle is compared to.
    """
    from ranx import Qrels, Run, compare, evaluate

    qrels = Qrels({f"q{index}": {"rel": 1} for index in range(len(gold))})
    runs: dict[str, Run] = {}
    for rerank, ppr in TOGGLES:
        with swept_settings(rerank=rerank, ppr=ppr):
            scores = await config_scores(gold, principal_id, k)
        runs[f"rerank={rerank},ppr={ppr}"] = Run(scores, name=f"rerank={rerank},ppr={ppr}")
    report = compare(qrels, list(runs.values()), metrics=metrics)
    scored = evaluate(qrels, runs[current], metrics)
    assert isinstance(scored, dict)  # a metric list always evaluates to a per-metric dict
    return ToggleSweepResult(
        per_config={label: float(report.results[label][metrics[0]]) for label in runs},
        headline={name: float(value) for name, value in scored.items()},
        comparison=str(report),
        significant_best=significant_winner(
            report.to_dict(), current, metrics[1], settings.self_improve_max_p
        ),
    )


async def judge_items(items: list[QA], principal_id: uuid.UUID, k: int) -> float | None:
    """Judge every item's recall for answerability and return the mean, null when judging is off.

    items: the full evaluation item set, gold and caller questions alike.
    principal_id: identity whose visibility scopes the recall.
    k: number of hits and seed facts each recall surfaces.
    """
    if not settings.eval_judge:
        return None
    judged = []
    for qa in items:
        result = await recall(qa.question, principal_id=principal_id, k=k)
        judged.append(await judge_answerable(qa.question, render_context(result)))
    return sum(judged) / len(judged) if judged else None


async def run_eval(
    questions: list[str] | None,
    k: int = 8,
    principal_id: uuid.UUID | None = None,
) -> EvalReport:
    """Measure recall quality on our own corpus, ranx metrics and an optional judge across toggles.

    For each rerank/ppr toggle, recalls every gold question and scores it into a ranx run against
    one shared qrels, so `ranx.compare` gives the per-config breakdown and significance table while
    `ranx.evaluate` gives the headline metrics under current settings. Judging runs only when
    `settings.eval_judge` is set, so the default stays cheap.

    questions: the caller's questions, or null to synthesize them from sampled facts.
    k: number of hits and seed facts each recall surfaces.
    principal_id: identity whose visibility scopes the recall and the sampled facts, the system
        principal when null.
    """
    principal_id = principal_id or settings.system_principal_id
    items = await build_questions(questions, principal_id)
    gold = [qa for qa in items if qa.expected is not None]
    metrics = [f"hit_rate@{k}", f"ndcg@{k}", "mrr"]
    current = f"rerank={settings.rerank},ppr={settings.ppr}"
    sweep = await sweep_toggles(gold, principal_id, k, metrics, current) if gold else None
    fixed_hit, routed_hit, routing_winner = (
        await routing_ab(gold, principal_id, k, metrics) if gold else (None, None, None)
    )
    mean_judge = await judge_items(items, principal_id, k)
    headline = sweep.headline if sweep else dict.fromkeys(metrics, 0.0)
    logger.info(
        "eval scored {n} items, hit@{k} {hit:.3f}", n=len(items), k=k, hit=headline[metrics[0]]
    )
    return EvalReport(
        n=len(items),
        hit_at_k=headline[metrics[0]],
        ndcg_at_k=headline[metrics[1]],
        mrr=headline[metrics[2]],
        mean_judge=mean_judge,
        per_config=sweep.per_config if sweep else {},
        comparison=sweep.comparison if sweep else None,
        significant_best=sweep.significant_best if sweep else None,
        fixed_hit_at_k=fixed_hit,
        routed_hit_at_k=routed_hit,
        routing_winner=routing_winner,
    )
