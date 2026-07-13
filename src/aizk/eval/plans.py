import time
import uuid
from collections import defaultdict
from collections.abc import Sequence
from enum import StrEnum, auto

import jinja2
from loguru import logger
from patos import FrozenModel
from ranx import Qrels, Run, evaluate
from sqlmodel import select

from ..answerability import judge_answerable
from ..config import settings
from ..extract import ontology
from ..extract.llm import structured
from ..retrieval import Candidate, Plan, recall
from ..store import Community, EntityClaim, EntityContent, LiveFact
from ..store.identity import User
from .gate import GateReport
from .harness import build_questions, render_context, swept_settings
from .models import GeneratedQuestion
from .routes import Route
from .sweep import percentile

_GLOBAL_QUESTION_SYSTEM = (
    "Turn the given theme summary into one broad, open question a person would ask to get an\n"
    "overview of that theme. Paraphrase it, never reusing the summary's own distinctive words,\n"
    "so answering the question needs the summary itself and not a surface word match. Write\n"
    "only the question, never referencing that you were given a summary."
)

_MULTIHOP_QUESTION_SYSTEM = (
    "You are given two connected facts that share one entity. Write one natural question whose\n"
    "answer requires combining both facts across that shared entity, so neither fact alone can\n"
    "answer it. Paraphrase, preferring descriptions over the facts' own key terms, and write\n"
    "only the question."
)

# Edges read per requested multihop question, ample slack for finding chained pairs.
_EDGE_CAP_FACTOR = 32


class Stratum(StrEnum):
    """The three question families the plan sweep stratifies over."""

    LOCAL = auto()
    GLOBAL = auto()
    MULTIHOP = auto()

    @property
    def route(self) -> Route:
        """The route a perfect router would pick for this stratum's questions."""
        return Route[self.name]


class StudyQuestion(FrozenModel):
    """One stratified probe whose answer needs every expected evidence text."""

    question: str
    expected: tuple[str, ...]
    stratum: Stratum


class GraphEdge(FrozenModel):
    """One live fact reduced to its endpoints and statement, the path builder's input."""

    subject_id: uuid.UUID
    object_id: uuid.UUID | None
    statement: str


class Arm(FrozenModel):
    """One sweep arm, a forced plan or the simulated router, plus a settings overlay."""

    name: str
    plan: Plan | None = None
    overrides: dict[str, bool | int | float] = {}


class ArmScore(FrozenModel):
    """One arm's ranking quality, judge verdict, and latency over one stratum."""

    arm: str
    hit_at_k: float
    ndcg_at_k: float
    mrr: float
    judge: float | None
    latency_p50_ms: float


class StratumResult(FrozenModel):
    """One stratum's scored arms."""

    stratum: Stratum
    n: int
    arms: list[ArmScore]


class RoutingReport(FrozenModel):
    """The live router's accuracy against the stratum labels, with the confusion counts."""

    n: int
    accuracy: float
    confusion: dict[str, dict[str, int]]


_TEMPLATE = jinja2.Template(
    """\
{%- if not strata %}
plan study scored no strata, no questions to evaluate
{%- else -%}
plan study k={{ k }}
{% for stratum in strata %}stratum={{ stratum.stratum }} n={{ stratum.n }}
{% for arm in stratum.arms %}  {{
    "{}: hit@{}={} ndcg@{}={} mrr={} p50={}ms{}".format(
        arm.arm, k, arm.hit_at_k, k, arm.ndcg_at_k, arm.mrr, arm.latency_p50_ms, arm.judge_note,
    )
}}
{% endfor %}{% endfor -%}
{% if seeding %}seed ablation over multihop n={{ seeding.n }}
{% for arm in seeding.arms %}  {{
    "{}: hit@{}={} ndcg@{}={} mrr={} p50={}ms{}".format(
        arm.arm, k, arm.hit_at_k, k, arm.ndcg_at_k, arm.mrr, arm.latency_p50_ms, arm.judge_note,
    )
}}
{% endfor %}{% endif -%}
{% if routing %}routing accuracy={{ routing.accuracy }} over n={{ routing.n }}
{% for stratum, row in routing.confusion.items() %}  {{ stratum }}: {{ row }}
{% endfor %}{% endif -%}
{% if gate %}{{ gate }}
{% endif -%}
{%- endif %}""",
    trim_blocks=True,
    lstrip_blocks=True,
)


def rounded_arm(arm: ArmScore) -> dict[str, float | str]:
    """One arm's row for the template, numbers rounded and the judge folded into a suffix."""
    return {
        "arm": arm.arm,
        "hit_at_k": round(arm.hit_at_k, 3),
        "ndcg_at_k": round(arm.ndcg_at_k, 3),
        "mrr": round(arm.mrr, 3),
        "latency_p50_ms": round(arm.latency_p50_ms, 1),
        "judge_note": f" judge={arm.judge:.3f}" if arm.judge is not None else "",
    }


class PlanStudyReport(FrozenModel):
    """The full stratified study, plan arms per stratum, seed ablations, routing, and gate."""

    k: int
    strata: list[StratumResult]
    seeding: StratumResult | None
    routing: RoutingReport | None
    gate: GateReport | None = None

    def render(self) -> str:
        """Render the study as a compact text table, one row per stratum and arm."""
        return _TEMPLATE.render(
            k=self.k,
            strata=[
                {
                    "stratum": result.stratum.value,
                    "n": result.n,
                    "arms": [rounded_arm(arm) for arm in result.arms],
                }
                for result in self.strata
            ],
            seeding=(
                {
                    "n": self.seeding.n,
                    "arms": [rounded_arm(arm) for arm in self.seeding.arms],
                }
                if self.seeding
                else None
            ),
            routing=(
                {
                    "accuracy": round(self.routing.accuracy, 3),
                    "n": self.routing.n,
                    "confusion": self.routing.confusion,
                }
                if self.routing
                else None
            ),
            gate=self.gate.render() if self.gate else None,
        ).strip()


def plan_arms() -> list[Arm]:
    """The five sweep arms, the three historical shapes, production's maximal plan, and
    the simulated router."""
    return [
        Arm(name="local", plan=Plan.focused()),
        Arm(name="global", plan=Plan.overview()),
        Arm(name="multihop", plan=Plan.multihop()),
        Arm(name="maximal", plan=Plan.maximal()),
        Arm(name="routed"),
    ]


def seeding_arms(floors: Sequence[float]) -> list[Arm]:
    """The R2 seed-ablation arms, every one under the forced multihop shape.

    floors: extra gate thresholds to sweep the mention extraction under.
    """
    plan = Plan.multihop()
    return [
        Arm(name="seeding=off", plan=plan, overrides={"graph_entity_seeding": False}),
        Arm(name="seeding=exact", plan=plan, overrides={"graph_mention_fuzzy": False}),
        Arm(name="seeding=exact+fuzzy", plan=plan, overrides={"graph_mention_fuzzy": True}),
        *(
            Arm(name=f"seed_floor={floor}", plan=plan, overrides={"gliner_gate_threshold": floor})
            for floor in floors
        ),
    ]


async def local_questions(user: User, n: int) -> list[StudyQuestion]:
    """The fact-paraphrase stratum, the harness's own gold question generation."""
    with swept_settings(eval_sample_questions=n):
        items = await build_questions(None, user)
    return [
        StudyQuestion(question=qa.question, expected=(qa.expected,), stratum=Stratum.LOCAL)
        for qa in items
        if qa.expected is not None
    ]


async def summary_pool(user: User, n: int) -> list[str]:
    """Community then RAPTOR summaries in stored order, the GLOBAL stratum's sources."""
    raptor_summary = EntityClaim.attributes >> "summary"
    async with user as session:
        communities = list(
            await session.exec(select(Community.summary).order_by(Community.id).limit(n))
        )
        raptor = list(
            await session.exec(
                select(raptor_summary)
                .join(EntityContent, EntityContent.id == EntityClaim.content_id)
                .where(EntityContent.type == ontology.RAPTOR_SUMMARY)
                .order_by(EntityClaim.id)
                .limit(n)
            )
        )
    return [*communities, *raptor][:n]


async def global_questions(user: User, n: int) -> list[StudyQuestion]:
    """The overview stratum, broad questions synthesized from stored theme summaries."""
    questions: list[StudyQuestion] = []
    for summary in await summary_pool(user, n):
        generated = await structured(_GLOBAL_QUESTION_SYSTEM, summary, GeneratedQuestion)
        questions.append(
            StudyQuestion(question=generated.question, expected=(summary,), stratum=Stratum.GLOBAL)
        )
    return questions


def two_hop_paths(edges: Sequence[GraphEdge], limit: int) -> list[tuple[str, str]]:
    """Statement pairs forming a two-hop path, fact A-B then fact B-C over the shared B.

    edges: the candidate facts in a deterministic order.
    limit: how many pairs to return at most.
    """
    by_subject: dict[uuid.UUID, list[GraphEdge]] = defaultdict(list)
    for edge in edges:
        by_subject[edge.subject_id].append(edge)
    paths: list[tuple[str, str]] = []
    for first in edges:
        if first.object_id is None:
            continue
        for second in by_subject[first.object_id]:
            backtrack = second is first or second.object_id == first.subject_id
            if backtrack or second.statement == first.statement:
                continue
            paths.append((first.statement, second.statement))
            if len(paths) == limit:
                return paths
    return paths


async def graph_edges(user: User, cap: int) -> list[GraphEdge]:
    """The visible connected fact edges in id order, capped, the multihop path source."""
    async with user as session:
        rows = await session.exec(
            select(LiveFact.subject_id, LiveFact.object_id, LiveFact.statement)
            .where(LiveFact.object_id.is_not(None))
            .order_by(LiveFact.id)
            .limit(cap)
        )
        return [
            GraphEdge(subject_id=subject, object_id=target, statement=statement)
            for subject, target, statement in rows
        ]


async def multihop_questions(user: User, n: int) -> list[StudyQuestion]:
    """The two-hop stratum, questions needing both facts of a connected pair."""
    edges = await graph_edges(user, n * _EDGE_CAP_FACTOR)
    questions: list[StudyQuestion] = []
    for first, second in two_hop_paths(edges, n):
        prompt = f"Fact one. {first}\nFact two. {second}"
        generated = await structured(_MULTIHOP_QUESTION_SYSTEM, prompt, GeneratedQuestion)
        questions.append(
            StudyQuestion(
                question=generated.question, expected=(first, second), stratum=Stratum.MULTIHOP
            )
        )
    return questions


async def stratum_questions(stratum: Stratum, user: User, n: int) -> list[StudyQuestion]:
    """Generate one stratum's probes from the stored corpus."""
    match stratum:
        case Stratum.LOCAL:
            return await local_questions(user, n)
        case Stratum.GLOBAL:
            return await global_questions(user, n)
        case _:
            return await multihop_questions(user, n)


def question_scores(question: StudyQuestion, result: Sequence[Candidate]) -> dict[str, float]:
    """Score one ranking, marking the first match of each expected text relevant."""
    texts = [candidate.line for candidate in result]
    matched: set[int] = set()
    scores: dict[str, float] = {}
    for rank, text in enumerate(texts):
        hit = next(
            (
                index
                for index, expected in enumerate(question.expected)
                if index not in matched and (text == expected or expected in text)
            ),
            None,
        )
        if hit is not None:
            matched.add(hit)
        scores[f"d{rank}" if hit is None else f"rel{hit}"] = float(len(texts) - rank)
    return scores


async def measure_arm(
    arm: Arm, questions: Sequence[StudyQuestion], user: User, k: int
) -> ArmScore:
    """Recall every probe under one arm and score ranking quality, judge, and latency.

    Each expected evidence text is its own relevance judgment, so a two-hop probe only
    scores fully when both of its facts surface. A plan-less arm simulates the retired
    query-time router by classifying each probe and forcing that route's shape, since
    production recall no longer routes on its own.
    """
    scores: dict[str, dict[str, float]] = {}
    latencies: list[float] = []
    verdicts: list[bool] = []
    with swept_settings(**arm.overrides):
        for index, question in enumerate(questions):
            start = time.perf_counter()
            plan = (
                arm.plan
                if arm.plan is not None
                else (await Route.classify(question.question)).plan
            )
            result = await recall(question.question, user=user, k=k, plan=plan)
            latencies.append((time.perf_counter() - start) * 1000.0)
            scores[f"q{index}"] = question_scores(question, result)
            if settings.eval_judge:
                verdicts.append(await judge_answerable(question.question, render_context(result)))
    qrels = Qrels(
        {
            f"q{index}": {f"rel{position}": 1 for position in range(len(question.expected))}
            for index, question in enumerate(questions)
        }
    )
    metrics = [f"hit_rate@{k}", f"ndcg@{k}", "mrr"]
    if any(scores.values()):
        values = evaluate(qrels, Run(scores, name=arm.name), metrics)
        assert isinstance(values, dict)  # a metric list always returns a per-metric mapping
    else:
        # ranx cannot build a run when every recall came back empty, an honest zero.
        values = dict.fromkeys(metrics, 0.0)
    return ArmScore(
        arm=arm.name,
        hit_at_k=values[metrics[0]],
        ndcg_at_k=values[metrics[1]],
        mrr=values[metrics[2]],
        judge=sum(verdicts) / len(verdicts) if verdicts else None,
        latency_p50_ms=percentile(latencies, 50),
    )


async def measure_routing(questions: Sequence[StudyQuestion]) -> RoutingReport:
    """Classify every probe live and compare the predicted route to its stratum label."""
    confusion: dict[str, dict[str, int]] = {}
    correct = 0
    for question in questions:
        route = await Route.classify(question.question)
        row = confusion.setdefault(question.stratum.value, {})
        row[route.name] = row.get(route.name, 0) + 1
        correct += route is question.stratum.route
    return RoutingReport(
        n=len(questions),
        accuracy=correct / len(questions) if questions else 0.0,
        confusion=confusion,
    )


async def run_plan_study(
    user: User | None = None,
    k: int = 8,
    per_stratum: int = 8,
    strata: Sequence[Stratum] = tuple(Stratum),
    seeding: bool = True,
    seed_floors: Sequence[float] = (0.3, 0.5),
) -> PlanStudyReport:
    """Sweep the plan arms over stratified probes, then the seed ablations and routing.

    Every model call rides the configured serving timeouts, and the strata parameter
    makes a re-run per stratum cheap. The routing report falls out of the same probes
    for free since each stratum labels its own ideal route.
    """
    user = user or User.system()
    results: list[StratumResult] = []
    by_stratum: dict[Stratum, list[StudyQuestion]] = {}
    for stratum in strata:
        questions = await stratum_questions(stratum, user, per_stratum)
        by_stratum[stratum] = questions
        arms = (
            [await measure_arm(arm, questions, user, k) for arm in plan_arms()]
            if questions
            else []
        )
        results.append(StratumResult(stratum=stratum, n=len(questions), arms=arms))
        logger.info(
            "plan study stratum {stratum} scored {arms} arms over {n} questions",
            stratum=stratum,
            arms=len(arms),
            n=len(questions),
        )
    pooled = [question for questions in by_stratum.values() for question in questions]
    multihop = by_stratum.get(Stratum.MULTIHOP, [])
    ablation = (
        StratumResult(
            stratum=Stratum.MULTIHOP,
            n=len(multihop),
            arms=[await measure_arm(arm, multihop, user, k) for arm in seeding_arms(seed_floors)],
        )
        if seeding and multihop
        else None
    )
    return PlanStudyReport(
        k=k,
        strata=results,
        seeding=ablation,
        routing=await measure_routing(pooled) if pooled else None,
    )
