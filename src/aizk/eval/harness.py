import contextlib
import math
from collections.abc import Generator, Sequence

from loguru import logger
from patos import FrozenModel
from ranx import Qrels, Run, compare, evaluate
from sqlmodel import select

from ..answerability import judge_answerable
from ..config import settings
from ..extract.llm import structured
from ..retrieval import Candidate, recall
from ..store import LiveFact
from ..store.identity import User
from .models import QA, EvalReport, GeneratedQuestion


@contextlib.contextmanager
def swept_settings(**fields: bool | int | float | str) -> Generator[None]:
    """Temporarily overlay fields onto the live settings singleton, restoring them after the
    block."""
    previous = {key: getattr(settings, key) for key in fields}
    for key, value in fields.items():
        setattr(settings, key, value)
    try:
        yield
    finally:
        for key, value in previous.items():
            setattr(settings, key, value)


_QUESTION_SYSTEM = (
    "Turn the given fact into one natural question a person would ask whose answer is that fact.\n"
    "Paraphrase it. Do not reuse the fact's own nouns, names, or key terms verbatim, reach for a\n"
    "synonym or a description instead, so answering the question needs the fact itself and not a\n"
    "surface word match. Write only the question, never referencing that you were given a fact."
)


def significant_winner(data: dict, current: str, metric: str, max_p: float) -> str | None:
    """The swept config whose metric significantly beats the current, or null on a tie or
    noise."""
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


def score_comparison(
    qrels: Qrels, runs: list[Run], metrics: list[str], baseline: str
) -> tuple[dict[str, dict[str, float]], str | None, str | None]:
    """Score named runs and compare them only when the sample supports significance."""
    if len(runs) < 2 or len(qrels) < 2:
        scored: dict[str, dict[str, float]] = {}
        for run in runs:
            values = evaluate(qrels, run, metrics)
            assert isinstance(values, dict)  # a metric list always returns a per-metric mapping
            scored[run.name] = values
        return scored, None, None
    report = compare(qrels, runs, metrics=metrics)
    scored = {run.name: report.results[run.name] for run in runs}
    winner = significant_winner(
        report.to_dict(), baseline, metrics[1], settings.self_improve_max_p
    )
    return scored, str(report), winner


async def sample_facts(user: User, n: int) -> list[str]:
    """Return up to n visible latest fact statements in a fixed order, the auto-eval source
    pool."""
    async with user as session:
        # `live_fact` already carries its own liveness gate, so this reads it directly rather than
        # opting the do_orm_execute listener out by hand the way a raw `FactClaim` read would.
        statements = await session.exec(select(LiveFact.statement).order_by(LiveFact.id).limit(n))
    return list(statements)


async def build_questions(
    questions: list[str] | None,
    user: User,
) -> list[QA]:
    """Assemble the evaluation items, the caller's questions or ones synthesized from facts."""
    if questions is not None:
        return [QA(question=question, expected=None) for question in questions]
    pairs: list[QA] = []
    for statement in await sample_facts(user, settings.eval_sample_questions):
        generated = await structured(_QUESTION_SYSTEM, statement, GeneratedQuestion)
        pairs.append(QA(question=generated.question, expected=statement))
    return pairs


def render_context(result: Sequence[Candidate]) -> str:
    """Flatten recalled candidates into the text the judge reads."""
    return "\n".join(candidate.line for candidate in result)


def retrieved_scores(qa: QA, result: Sequence[Candidate]) -> dict[str, float]:
    """Score the returned evidence order and mark the expected fact's first match relevant."""
    texts = [candidate.line for candidate in result]
    scores: dict[str, float] = {}
    matched = False
    for rank, text in enumerate(texts):
        hit = qa.expected is not None and (text == qa.expected or qa.expected in text)
        relevant = hit and not matched
        matched = matched or hit
        scores["rel" if relevant else f"d{rank}"] = float(len(texts) - rank)
    return scores


async def config_scores(gold: list[QA], user: User, k: int) -> dict[str, dict[str, float]]:
    """Recall every gold question under one config and score each ranking into a ranx run."""
    scores: dict[str, dict[str, float]] = {}
    for index, qa in enumerate(gold):
        result = await recall(qa.question, user=user, k=k)
        scores[f"q{index}"] = retrieved_scores(qa, result)
    return scores


class ToggleSweepResult(FrozenModel):
    """The SQL multihop toggle sweep's scored outcome."""

    per_config: dict[str, float]
    headline: dict[str, float]
    comparison: str | None
    significant_best: str | None


async def sweep_toggles(
    gold: list[QA], user: User, k: int, metrics: list[str], current: str
) -> ToggleSweepResult:
    """Score multihop recall on and off against one shared set of relevance judgments."""
    qrels = Qrels({f"q{index}": {"rel": 1} for index in range(len(gold))})
    runs: dict[str, Run] = {}
    for hops in sorted({0, settings.multihop_max_hops}):
        with swept_settings(multihop_max_hops=hops):
            scores = await config_scores(gold, user, k)
        label = f"multihop_max_hops={hops}"
        runs[label] = Run(scores, name=label)
    scored, comparison, significant_best = score_comparison(
        qrels, list(runs.values()), metrics, current
    )
    return ToggleSweepResult(
        per_config={label: float(scored[label][metrics[0]]) for label in runs},
        headline=scored[current],
        comparison=comparison,
        significant_best=significant_best,
    )


async def judge_items(items: list[QA], user: User, k: int) -> float | None:
    """Judge every item's recall for answerability and return the mean, null when judging is
    off."""
    if not settings.eval_judge:
        return None
    judged = []
    for qa in items:
        result = await recall(qa.question, user=user, k=k)
        judged.append(await judge_answerable(qa.question, render_context(result)))
    return sum(judged) / len(judged) if judged else None


async def run_eval(
    questions: list[str] | None,
    k: int = 8,
    user: User | None = None,
) -> EvalReport:
    """Measure recall quality on our own corpus, ranx metrics and an optional judge across
    toggles."""
    user = user or User.system()
    items = await build_questions(questions, user)
    gold = [qa for qa in items if qa.expected is not None]
    metrics = [f"hit_rate@{k}", f"ndcg@{k}", "mrr"]
    current = f"multihop_max_hops={settings.multihop_max_hops}"
    sweep = await sweep_toggles(gold, user, k, metrics, current) if gold else None
    mean_judge = await judge_items(items, user, k)
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
    )
