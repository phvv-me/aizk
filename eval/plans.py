import time
from collections import defaultdict
from collections.abc import Sequence
from enum import StrEnum, auto
from types import TracebackType
from typing import Self

import jinja2
from loguru import logger
from patos import FrozenModel
from pydantic import UUID5
from ranx import Qrels, Run, evaluate
from sqlmodel import select

from aizk.config import settings
from aizk.ontology import System
from aizk.retrieval import Candidate, ContextPack, Plan, recall
from aizk.serving.extract import LLM
from aizk.store import Community, Entity, Fact
from aizk.store.identity import User

from .answerability import judge_answerable
from .config import settings as eval_settings
from .gate import GateReport
from .metrics import percentile
from .models import GeneratedQuestion
from .routes import Route

_LOCAL_QUESTION_SYSTEM = (
    "Turn the given fact into one natural question whose answer is that fact.\n"
    "Keep the proper nouns, product names, and qualifiers needed to identify the subject.\n"
    "Paraphrase the relationship and surrounding wording without making the question vague.\n"
    "Write only the question and never mention that a fact was provided."
)

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
    """The three question families the retrieval benchmark stratifies over."""

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

    subject_id: UUID5
    object_id: UUID5 | None
    statement: str


class SettingsOverlay:
    """Temporarily apply one diagnostic settings overlay."""

    def __init__(self, fields: dict[str, bool | int | float]) -> None:
        self.fields = fields
        self.previous: dict[str, bool | int | float] = {}
        for name in fields:
            value = getattr(settings, name)
            if not isinstance(value, bool | int | float):
                raise TypeError(f"{name} is not a numeric diagnostic setting")
            self.previous[name] = value

    def __enter__(self) -> Self:
        for name, value in self.fields.items():
            setattr(settings, name, value)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc, traceback
        for name, value in self.previous.items():
            setattr(settings, name, value)


class Arm(FrozenModel):
    """One study arm, a forced plan or the simulated router, plus a settings overlay."""

    name: str
    plan: Plan | None = None
    overrides: dict[str, bool | int | float] = {}

    @classmethod
    def production(cls) -> Arm:
        """The only plan used by production recall."""
        return cls(name="maximal", plan=Plan.maximal())

    @classmethod
    def historical(cls) -> tuple[Arm, ...]:
        """Production and the historical forced and routed comparison arms."""
        return (
            cls(name="local", plan=Plan.focused()),
            cls(name="global", plan=Plan.overview()),
            cls(name="multihop", plan=Plan.multihop()),
            cls.production(),
            cls(name="routed"),
        )

    @classmethod
    def seeding(cls, floors: Sequence[float]) -> tuple[Arm, ...]:
        """The multihop seeding ablations used only by the diagnostic study."""
        plan = Plan.multihop()
        return (
            cls(name="seeding=off", plan=plan, overrides={"graph_entity_seeding": False}),
            cls(name="seeding=exact", plan=plan, overrides={"graph_mention_fuzzy": False}),
            cls(name="seeding=exact+fuzzy", plan=plan, overrides={"graph_mention_fuzzy": True}),
            *(
                cls(
                    name=f"seed_floor={floor}",
                    plan=plan,
                    overrides={"gliner_gate_threshold": floor},
                )
                for floor in floors
            ),
        )


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
{{ title }} scored no strata, no questions to evaluate
{%- else -%}
{{ title }} k={{ k }}
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
    title: str = "plan study"

    def render(self) -> str:
        """Render the study as a compact text table, one row per stratum and arm."""
        return _TEMPLATE.render(
            k=self.k,
            title=self.title,
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


async def local_questions(user: User, n: int) -> list[StudyQuestion]:
    """Generate local probes from visible current facts."""
    async with user as session:
        statements = list(
            await session.exec(select(Fact.Live.statement).order_by(Fact.Live.id).limit(n))
        )
    return [
        StudyQuestion(
            question=(
                await LLM.configured().generate(
                    _LOCAL_QUESTION_SYSTEM,
                    statement,
                    GeneratedQuestion,
                )
            ).question,
            expected=(statement,),
            stratum=Stratum.LOCAL,
        )
        for statement in statements
    ]


async def summary_pool(user: User, n: int) -> list[str]:
    """Community then RAPTOR summaries in stored order, the GLOBAL stratum's sources."""
    raptor_summary = Entity.Claim.attributes >> "summary"
    async with user as session:
        communities = list(
            await session.exec(select(Community.summary).order_by(Community.id).limit(n))
        )
        raptor = list(
            await session.exec(
                select(raptor_summary)
                .join(Entity.Content, Entity.Content.id == Entity.Claim.content_id)
                .where(Entity.Content.type == System.Entity.RAPTOR_SUMMARY)
                .order_by(Entity.Claim.id)
                .limit(n)
            )
        )
    return list(dict.fromkeys([*communities, *raptor]))[:n]


async def global_questions(user: User, n: int) -> list[StudyQuestion]:
    """The overview stratum, broad questions synthesized from stored theme summaries."""
    questions: list[StudyQuestion] = []
    for summary in await summary_pool(user, n):
        generated = await LLM.configured().generate(
            _GLOBAL_QUESTION_SYSTEM,
            summary,
            GeneratedQuestion,
        )
        questions.append(
            StudyQuestion(question=generated.question, expected=(summary,), stratum=Stratum.GLOBAL)
        )
    return questions


def two_hop_paths(edges: Sequence[GraphEdge], limit: int) -> list[tuple[str, str]]:
    """Statement pairs forming a two-hop path, fact A-B then fact B-C over the shared B.

    edges: the candidate facts in a deterministic order.
    limit: how many pairs to return at most.
    """
    by_subject: dict[UUID5, list[GraphEdge]] = defaultdict(list)
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
            select(Fact.Live.subject_id, Fact.Live.object_id, Fact.Live.statement)
            .where(Fact.Live.object_id.is_not(None))
            .order_by(Fact.Live.id)
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
        generated = await LLM.configured().generate(
            _MULTIHOP_QUESTION_SYSTEM,
            prompt,
            GeneratedQuestion,
        )
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
    with SettingsOverlay(arm.overrides):
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
            if eval_settings.judge:
                context = ContextPack.from_candidates(result).text
                verdicts.append(await judge_answerable(question.question, context))
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


class RetrievalBenchmark:
    """Generate stratified probes and evaluate explicit retrieval plans."""

    def __init__(
        self,
        user: User | None = None,
        k: int = 8,
        per_stratum: int = 8,
        strata: Sequence[Stratum] = tuple(Stratum),
    ) -> None:
        self.user = user or User.system()
        self.k = k
        self.per_stratum = per_stratum
        self.strata = tuple(strata)

    async def production(self) -> PlanStudyReport:
        """Benchmark the maximal plan used by every production recall."""
        report = await self.run(
            arms=(Arm.production(),),
            title="production retrieval",
        )
        if not any(result.n for result in report.strata):
            raise ValueError("benchmark found no visible evidence, select the corpus owner")
        return report

    async def diagnostic(
        self,
        seeding: bool = True,
        seed_floors: Sequence[float] = (0.3, 0.5),
    ) -> PlanStudyReport:
        """Compare historical plans, routing, and optional graph seeding ablations."""
        return await self.run(
            arms=Arm.historical(),
            seed_arms=Arm.seeding(seed_floors) if seeding else (),
            routing=True,
            title="plan study",
        )

    async def run(
        self,
        arms: Sequence[Arm],
        seed_arms: Sequence[Arm] = (),
        routing: bool = False,
        title: str = "retrieval benchmark",
    ) -> PlanStudyReport:
        """Generate each stratum once, then score every requested arm over it."""
        results: list[StratumResult] = []
        questions_by_stratum: dict[Stratum, list[StudyQuestion]] = {}
        for stratum in self.strata:
            questions = await stratum_questions(stratum, self.user, self.per_stratum)
            questions_by_stratum[stratum] = questions
            scores = (
                [await measure_arm(arm, questions, self.user, self.k) for arm in arms]
                if questions
                else []
            )
            results.append(StratumResult(stratum=stratum, n=len(questions), arms=scores))
            logger.info(
                "{title} stratum {stratum} scored {arms} arms over {n} questions",
                title=title,
                stratum=stratum,
                arms=len(scores),
                n=len(questions),
            )
        pooled = [
            question for questions in questions_by_stratum.values() for question in questions
        ]
        multihop = questions_by_stratum.get(Stratum.MULTIHOP, [])
        ablation = (
            StratumResult(
                stratum=Stratum.MULTIHOP,
                n=len(multihop),
                arms=[await measure_arm(arm, multihop, self.user, self.k) for arm in seed_arms],
            )
            if seed_arms and multihop
            else None
        )
        return PlanStudyReport(
            k=self.k,
            strata=results,
            seeding=ablation,
            routing=await measure_routing(pooled) if routing and pooled else None,
            title=title,
        )
