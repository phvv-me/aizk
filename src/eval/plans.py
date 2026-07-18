import re
import time
from collections import defaultdict
from collections.abc import Sequence
from enum import StrEnum, auto
from types import TracebackType
from typing import Self

import jinja2
from ir_measures import RR, Success, iter_calc, nDCG
from loguru import logger
from patos import FrozenModel
from pydantic import UUID5
from sqlalchemy.orm import aliased
from sqlmodel import select

from aizk.config import settings
from aizk.ontology import System
from aizk.retrieval import Candidate, Plan, RecallResult, recall
from aizk.retrieval.packing import pack
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
_JUDGE_CONTEXT_BUDGET = 1024

_GLOBAL_QUESTION_SYSTEM = (
    "Turn the given theme summary into one broad, open question a person would ask to get an\n"
    "overview of that theme. Paraphrase it, never reusing the summary's own distinctive words,\n"
    "so answering the question needs the summary itself and not a surface word match. Write\n"
    "only the question, never referencing that you were given a summary."
)

_MULTIHOP_QUESTION_SYSTEM = (
    "You are given two connected facts that share one entity. Write one natural question whose\n"
    "answer requires combining both facts across that shared entity, so neither fact alone can\n"
    "answer it. The prompt names a required starting anchor and bridge. Include both strings\n"
    "verbatim in the question so the path is identifiable. Never replace either with a vague\n"
    "phrase such as the entity, the system, the space, the category, the group, or the thing.\n"
    "Paraphrase only the relationship wording and write only the question."
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
    id: str = ""


class GraphEdge(FrozenModel):
    """One live fact reduced to its endpoints and statement, the path builder's input."""

    subject_id: UUID5
    subject_name: str
    object_id: UUID5 | None
    object_name: str
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
    def ablations(cls) -> tuple[Arm, ...]:
        """The production plan, three recall removals, and the evidence floor."""
        return (
            cls.production(),
            cls(name="maximal_without_raptor", plan=Plan.maximal_without_raptor()),
            cls(
                name="maximal_without_communities",
                plan=Plan.maximal_without_communities(),
            ),
            cls(name="maximal_without_profiles", plan=Plan.maximal_without_profiles()),
            cls(name="focused", plan=Plan.focused()),
        )

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


class QueryResult(FrozenModel):
    """One arm, stratum, and question observation retained for paired analysis."""

    arm: str
    stratum: Stratum
    question_id: str
    question: str
    rank_first_relevant: int | None
    hit_at_k: float
    ndcg_at_k: float
    mrr: float
    judge: float | None
    latency_ms: float


class ArmScore(FrozenModel):
    """One arm's aggregate display score plus its retained question rows."""

    arm: str
    hit_at_k: float
    ndcg_at_k: float
    mrr: float
    judge: float | None
    latency_p50_ms: float
    rows: tuple[QueryResult, ...] = ()


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
    """The full stratified study with retained long-format question rows."""

    k: int
    strata: list[StratumResult]
    seeding: StratumResult | None
    routing: RoutingReport | None
    rows: tuple[QueryResult, ...] = ()
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
                await LLM.from_settings(settings).generate(
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
        generated = await LLM.from_settings(settings).generate(
            _GLOBAL_QUESTION_SYSTEM,
            summary,
            GeneratedQuestion,
        )
        questions.append(
            StudyQuestion(question=generated.question, expected=(summary,), stratum=Stratum.GLOBAL)
        )
    return questions


def two_hop_paths(edges: Sequence[GraphEdge], limit: int) -> list[tuple[GraphEdge, GraphEdge]]:
    """Edge pairs forming a two-hop path, fact A-B then fact B-C over the shared B.

    edges: the candidate facts in a deterministic order.
    limit: how many pairs to return at most.
    """
    by_subject: dict[UUID5, list[GraphEdge]] = defaultdict(list)
    for edge in edges:
        by_subject[edge.subject_id].append(edge)
    paths: list[tuple[GraphEdge, GraphEdge]] = []
    for first in edges:
        if first.object_id is None:
            continue
        for second in by_subject[first.object_id]:
            backtrack = second is first or second.object_id == first.subject_id
            if backtrack or second.statement == first.statement:
                continue
            paths.append((first, second))
            if len(paths) == limit:
                return paths
    return paths


async def graph_edges(user: User, cap: int) -> list[GraphEdge]:
    """The visible connected fact edges in id order, capped, the multihop path source."""
    subject = aliased(Entity.Content, name="subject")
    target = aliased(Entity.Content, name="target")
    async with user as session:
        rows = await session.exec(
            select(
                Fact.Live.subject_id,
                subject.name,
                Fact.Live.object_id,
                target.name,
            )
            .add_columns(Fact.Live.statement)
            .join(subject, subject.id == Fact.Live.subject_id)
            .join(target, target.id == Fact.Live.object_id)
            .where(Fact.Live.object_id.is_not(None))
            .order_by(Fact.Live.id)
            .limit(cap)
        )
        return [
            GraphEdge(
                subject_id=subject_id,
                subject_name=subject_name,
                object_id=object_id,
                object_name=object_name,
                statement=statement,
            )
            for subject_id, subject_name, object_id, object_name, statement in rows
        ]


def mentions(text: str, phrase: str) -> bool:
    """Whether `text` contains every word of `phrase` as one contiguous case-folded run.

    Word-run matching rather than a substring so a short anchor like `AI` never matches
    inside `brain`, and a punctuation-only phrase never spuriously anchors a question.
    """
    haystack = re.findall(r"\w+", text.casefold())
    needle = re.findall(r"\w+", phrase.casefold())
    if not needle:
        return False
    return any(
        haystack[start : start + len(needle)] == needle
        for start in range(len(haystack) - len(needle) + 1)
    )


async def multihop_questions(user: User, n: int) -> list[StudyQuestion]:
    """The two-hop stratum, distinct questions each needing both facts of a connected pair."""
    edges = await graph_edges(user, n * _EDGE_CAP_FACTOR)
    questions: list[StudyQuestion] = []
    seen: set[str] = set()
    for first, second in two_hop_paths(edges, n * 4):
        bridge = first.object_name
        prompt = (
            f"Required starting anchor. {first.subject_name}\n"
            f"Required bridge. {bridge}\n"
            f"Fact one. {first.statement}\n"
            f"Fact two. {second.statement}"
        )
        generated = await LLM.from_settings(settings).generate(
            _MULTIHOP_QUESTION_SYSTEM,
            prompt,
            GeneratedQuestion,
        )
        if not mentions(generated.question, first.subject_name) or not mentions(
            generated.question, bridge
        ):
            logger.warning(
                "discarded unanchored multihop question for {subject} through {bridge}",
                subject=first.subject_name,
                bridge=bridge,
            )
            continue
        normalized = " ".join(generated.question.casefold().split())
        if normalized in seen:
            continue
        seen.add(normalized)
        questions.append(
            StudyQuestion(
                question=generated.question,
                expected=(first.statement, second.statement),
                stratum=Stratum.MULTIHOP,
            )
        )
        if len(questions) == n:
            break
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


def ranking_metrics(
    question: StudyQuestion,
    scores: dict[str, float],
    k: int,
) -> tuple[int | None, float, float, float]:
    """Calculate native per-query rank, hit, nDCG, and reciprocal rank."""
    query_id = "q"
    hit_measure = Success @ k
    ndcg_measure = nDCG @ k
    measures = (hit_measure, ndcg_measure, RR)
    values = {
        str(value.measure): float(value.value)
        for value in iter_calc(
            measures,
            {query_id: {f"rel{position}": 1 for position in range(len(question.expected))}},
            {query_id: scores},
        )
    }
    rank = next(
        (position for position, document in enumerate(scores, 1) if document.startswith("rel")),
        None,
    )
    return (
        rank,
        values.get(str(hit_measure), 0.0),
        values.get(str(ndcg_measure), 0.0),
        values.get(str(RR), 0.0),
    )


async def measure_arm(
    arm: Arm,
    questions: Sequence[StudyQuestion],
    user: User,
    k: int,
    judge: bool | None = None,
) -> ArmScore:
    """Recall every probe and retain its paired ranking, judge, and latency row."""
    rows: list[QueryResult] = []
    judge_enabled = eval_settings.judge if judge is None else judge
    with SettingsOverlay(arm.overrides):
        for index, question in enumerate(questions):
            start = time.perf_counter()
            plan = (
                arm.plan
                if arm.plan is not None
                else (await Route.classify(question.question)).plan
            )
            result = await recall(question.question, user=user, k=k, plan=plan)
            latency_ms = (time.perf_counter() - start) * 1000.0
            scores = question_scores(question, result)
            rank, hit_at_k, ndcg_at_k, mrr = ranking_metrics(question, scores, k)
            verdict: float | None = None
            if judge_enabled:
                context = await RecallResult.from_candidates(
                    pack(result, _JUDGE_CONTEXT_BUDGET)
                ).to_markdown()
                verdict = float(await judge_answerable(question.question, context))
            rows.append(
                QueryResult(
                    arm=arm.name,
                    stratum=question.stratum,
                    question_id=question.id or f"{question.stratum.value}:{index:04d}",
                    question=question.question,
                    rank_first_relevant=rank,
                    hit_at_k=hit_at_k,
                    ndcg_at_k=ndcg_at_k,
                    mrr=mrr,
                    judge=verdict,
                    latency_ms=latency_ms,
                )
            )
    judged = [row.judge for row in rows if row.judge is not None]
    count = len(rows)
    return ArmScore(
        arm=arm.name,
        hit_at_k=sum(row.hit_at_k for row in rows) / count if rows else 0.0,
        ndcg_at_k=sum(row.ndcg_at_k for row in rows) / count if rows else 0.0,
        mrr=sum(row.mrr for row in rows) / count if rows else 0.0,
        judge=sum(judged) / len(judged) if judged else None,
        latency_p50_ms=percentile([row.latency_ms for row in rows], 50),
        rows=tuple(rows),
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
        questions: Sequence[StudyQuestion] | None = None,
        judge: bool | None = None,
    ) -> None:
        self.user = user or User.system()
        self.k = k
        self.per_stratum = per_stratum
        self.strata = tuple(strata)
        self.questions = tuple(questions) if questions is not None else None
        self.judge = judge

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
            arms=Arm.ablations(),
            seed_arms=Arm.seeding(seed_floors) if seeding else (),
            routing=True,
            title="plan ablation study",
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
        rows: list[QueryResult] = []
        questions_by_stratum: dict[Stratum, list[StudyQuestion]] = {}
        for stratum in self.strata:
            questions = (
                [question for question in self.questions if question.stratum is stratum]
                if self.questions is not None
                else await stratum_questions(stratum, self.user, self.per_stratum)
            )
            questions_by_stratum[stratum] = questions
            scores = (
                [
                    await measure_arm(
                        arm,
                        questions,
                        self.user,
                        self.k,
                        judge=self.judge,
                    )
                    for arm in arms
                ]
                if questions
                else []
            )
            rows.extend(row for score in scores for row in score.rows)
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
        ablation: StratumResult | None = None
        if seed_arms and multihop:
            seed_scores = [
                await measure_arm(
                    arm,
                    multihop,
                    self.user,
                    self.k,
                    judge=self.judge,
                )
                for arm in seed_arms
            ]
            rows.extend(row for score in seed_scores for row in score.rows)
            ablation = StratumResult(
                stratum=Stratum.MULTIHOP,
                n=len(multihop),
                arms=seed_scores,
            )
        return PlanStudyReport(
            k=self.k,
            strata=results,
            seeding=ablation,
            routing=await measure_routing(pooled) if routing and pooled else None,
            rows=tuple(rows),
            title=title,
        )
