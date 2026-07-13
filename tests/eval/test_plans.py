import uuid
from importlib import import_module

import dbutil
import pytest
import seedgraph
from eval_util import fact_bundle, install_constant_recall
from hypothesis import given
from hypothesis import strategies as st

from aizk.config import settings
from aizk.eval import QA, GeneratedQuestion, PlanStudyReport, Stratum, StudyQuestion
from aizk.eval.gate import GateReport
from aizk.eval.plans import (
    Arm,
    ArmScore,
    GraphEdge,
    RoutingReport,
    StratumResult,
    graph_edges,
    measure_arm,
    measure_routing,
    plan_arms,
    question_scores,
    run_plan_study,
    seeding_arms,
    stratum_questions,
    summary_pool,
    two_hop_paths,
)
from aizk.eval.routes import Route
from aizk.extract import ontology
from aizk.retrieval import Candidate, Plan
from aizk.store import Community, EntityClaim, EntityContent
from aizk.store.identity import User

plans = import_module("aizk.eval.plans")
routes_module = import_module("aizk.eval.routes")


def study_question(
    question: str = "q", expected: tuple[str, ...] = ("a",), stratum: Stratum = Stratum.LOCAL
) -> StudyQuestion:
    return StudyQuestion(question=question, expected=expected, stratum=stratum)


def install_structured(
    monkeypatch: pytest.MonkeyPatch, question: str = "generated?"
) -> list[tuple[str, str]]:
    """Answer every question-generation call and record the (system, user) prompts."""
    calls: list[tuple[str, str]] = []

    async def structured(
        system: str, user: str, schema: type[GeneratedQuestion]
    ) -> GeneratedQuestion:
        calls.append((system, user))
        assert schema is GeneratedQuestion
        return GeneratedQuestion(question=question)

    monkeypatch.setattr(plans, "structured", structured)
    return calls


def install_route(monkeypatch: pytest.MonkeyPatch, route: Route) -> list[str]:
    """Route every live classification to one route and record the queries."""
    queries: list[str] = []

    async def classify(text: str, task: str, labels: type[Route]) -> Route:
        queries.append(text)
        return route

    monkeypatch.setattr(routes_module, "classify", classify)
    return queries


def test_each_stratum_labels_its_own_ideal_route() -> None:
    assert Stratum.LOCAL.route is Route.LOCAL
    assert Stratum.GLOBAL.route is Route.GLOBAL
    assert Stratum.MULTIHOP.route is Route.MULTIHOP


def test_route_uses_the_gliner2_classification_head(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str, type[Route]]] = []

    async def classify(text: str, task: str, labels: type[Route]) -> Route:
        calls.append((text, task, labels))
        return Route.MULTIHOP

    monkeypatch.setattr(routes_module, "classify", classify)

    assert dbutil.run(Route.classify("How are A and B connected?")) is Route.MULTIHOP
    assert calls == [("How are A and B connected?", "memory retrieval route", Route)]


def test_each_route_maps_to_its_historical_plan_shape() -> None:
    assert Route.LOCAL.plan == Plan.focused()
    assert Route.GLOBAL.plan == Plan.overview()
    assert Route.MULTIHOP.plan == Plan.multihop()


def test_plan_arms_force_each_shape_and_leave_only_routed_live() -> None:
    arms = plan_arms()

    assert [arm.name for arm in arms] == ["local", "global", "multihop", "maximal", "routed"]
    assert arms[0].plan == Plan.focused()
    assert arms[1].plan == Plan.overview()
    assert arms[2].plan == Plan.multihop()
    assert arms[3].plan == Plan.maximal()
    assert arms[4].plan is None
    assert all(arm.overrides == {} for arm in arms)


def test_seeding_arms_ablate_seeding_fuzz_and_the_floor_under_a_forced_plan() -> None:
    arms = seeding_arms((0.3, 0.5))

    assert [arm.name for arm in arms] == [
        "seeding=off",
        "seeding=exact",
        "seeding=exact+fuzzy",
        "seed_floor=0.3",
        "seed_floor=0.5",
    ]
    assert all(arm.plan == Plan.multihop() for arm in arms)
    assert arms[0].overrides == {"graph_entity_seeding": False}
    assert arms[1].overrides == {"graph_mention_fuzzy": False}
    assert arms[2].overrides == {"graph_mention_fuzzy": True}
    assert arms[3].overrides == {"gliner_gate_threshold": 0.3}


_POOL = [uuid.UUID(int=index) for index in range(1, 7)]


@st.composite
def synthetic_edges(draw: st.DrawFn) -> list[GraphEdge]:
    count = draw(st.integers(min_value=0, max_value=12))
    return [
        GraphEdge(
            subject_id=draw(st.sampled_from(_POOL)),
            object_id=draw(st.none() | st.sampled_from(_POOL)),
            statement=f"statement {index}",
        )
        for index in range(count)
    ]


@given(edges=synthetic_edges(), limit=st.integers(min_value=1, max_value=8))
def test_two_hop_paths_are_true_chains_within_the_limit(
    edges: list[GraphEdge], limit: int
) -> None:
    by_statement = {edge.statement: edge for edge in edges}
    paths = two_hop_paths(edges, limit)

    assert len(paths) <= limit
    for first_text, second_text in paths:
        first, second = by_statement[first_text], by_statement[second_text]
        assert first.object_id is not None
        assert second.subject_id == first.object_id
        assert second.object_id != first.subject_id
        assert second.statement != first.statement


def test_two_hop_paths_chains_forward_and_refuses_backtracks() -> None:
    a, b, c = _POOL[:3]
    chain = [
        GraphEdge(subject_id=a, object_id=b, statement="a to b"),
        GraphEdge(subject_id=b, object_id=c, statement="b to c"),
    ]
    cycle = [
        GraphEdge(subject_id=a, object_id=b, statement="a to b"),
        GraphEdge(subject_id=b, object_id=a, statement="b to a"),
    ]
    loop = [GraphEdge(subject_id=a, object_id=a, statement="a to a")]

    assert two_hop_paths(chain, 4) == [("a to b", "b to c")]
    assert two_hop_paths(cycle, 4) == []
    assert two_hop_paths(loop, 4) == []
    assert two_hop_paths(
        chain + [GraphEdge(subject_id=b, object_id=c, statement="b again")], 1
    ) == [("a to b", "b to c")]


@given(
    texts=st.lists(st.text(min_size=1, max_size=12), max_size=8),
    expected=st.lists(st.text(min_size=1, max_size=12), min_size=1, max_size=3),
)
def test_question_scores_weights_every_rank_and_marks_each_expected_once(
    texts: list[str], expected: list[str]
) -> None:
    question = study_question(expected=tuple(expected))
    result = fact_bundle(texts)

    scores = question_scores(question, result)

    assert len(scores) == len(texts)
    assert sorted(scores.values()) == [float(weight) for weight in range(1, len(texts) + 1)]
    relevant = [key for key in scores if key.startswith("rel")]
    assert len(relevant) == len(set(relevant))
    assert all(int(key.removeprefix("rel")) < len(expected) for key in relevant)


def test_question_scores_matches_each_expected_to_its_first_hit() -> None:
    question = study_question(expected=("first edge", "second edge"))
    result = fact_bundle(["the first edge holds", "noise", "a second edge holds"])

    scores = question_scores(question, result)

    assert scores == {"rel0": 3.0, "d1": 2.0, "rel1": 1.0}


def test_question_scores_consumes_one_expected_per_text() -> None:
    question = study_question(expected=("edge", "edge"))
    result = fact_bundle(["the edge holds"])

    assert question_scores(question, result) == {"rel0": 1.0}


def test_measure_arm_scores_ranking_latency_and_the_swept_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen_hops: list[int] = []
    forced = Plan.multihop()

    async def stub_recall(
        query: str,
        user: User,
        k: int = 8,
        token_budget: int | None = None,
        plan: Plan | None = None,
    ) -> tuple[Candidate, ...]:
        assert plan == forced
        seen_hops.append(settings.multihop_max_hops)
        return fact_bundle(["alpha holds", "noise"])

    monkeypatch.setattr(plans, "recall", stub_recall)
    arm = Arm(name="probe", plan=forced, overrides={"multihop_max_hops": 7})
    questions = [
        study_question(question="what holds", expected=("alpha holds",)),
        study_question(question="what fell", expected=("never present",)),
    ]

    score = dbutil.run(measure_arm(arm, questions, User.system(), 4))

    assert seen_hops == [7, 7]
    assert settings.multihop_max_hops != 7  # the overlay restored itself
    assert score.arm == "probe"
    assert score.hit_at_k == 0.5
    assert 0.0 < score.ndcg_at_k < 1.0
    assert score.mrr == 0.5
    assert score.judge is None
    assert score.latency_p50_ms >= 0.0


def test_measure_arm_zeroes_the_metrics_when_every_recall_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def empty_recall(
        query: str,
        user: User,
        k: int = 8,
        token_budget: int | None = None,
        plan: Plan | None = None,
    ) -> tuple[Candidate, ...]:
        return ()

    monkeypatch.setattr(plans, "recall", empty_recall)
    install_route(monkeypatch, Route.LOCAL)

    score = dbutil.run(measure_arm(Arm(name="routed"), [study_question()], User.system(), 4))

    assert (score.hit_at_k, score.ndcg_at_k, score.mrr) == (0.0, 0.0, 0.0)


def test_a_plan_less_arm_simulates_the_router_per_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forced: list[Plan | None] = []

    async def stub_recall(
        query: str,
        user: User,
        k: int = 8,
        token_budget: int | None = None,
        plan: Plan | None = None,
    ) -> tuple[Candidate, ...]:
        forced.append(plan)
        return fact_bundle(["alpha holds"])

    monkeypatch.setattr(plans, "recall", stub_recall)
    queries = install_route(monkeypatch, Route.GLOBAL)
    questions = [study_question(question="one"), study_question(question="two")]

    dbutil.run(measure_arm(Arm(name="routed"), questions, User.system(), 4))

    assert queries == ["one", "two"]
    assert forced == [Plan.overview(), Plan.overview()]


def test_measure_arm_judges_when_the_judge_is_on(monkeypatch: pytest.MonkeyPatch) -> None:
    install_constant_recall(monkeypatch, plans, "alpha holds")
    install_route(monkeypatch, Route.LOCAL)
    monkeypatch.setattr(settings, "eval_judge", True)
    judged: list[tuple[str, str]] = []

    async def judge(question: str, context: str) -> bool:
        judged.append((question, context))
        return len(judged) == 1

    monkeypatch.setattr(plans, "judge_answerable", judge)
    questions = [study_question(question="one"), study_question(question="two")]

    score = dbutil.run(measure_arm(Arm(name="routed"), questions, User.system(), 4))

    assert score.judge == 0.5
    assert [question for question, _ in judged] == ["one", "two"]
    assert all(context == "alpha holds" for _, context in judged)


def test_measure_routing_scores_accuracy_and_the_confusion_matrix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_route(monkeypatch, Route.LOCAL)
    questions = [
        study_question(stratum=Stratum.LOCAL),
        study_question(stratum=Stratum.GLOBAL),
        study_question(stratum=Stratum.MULTIHOP),
        study_question(stratum=Stratum.MULTIHOP),
    ]

    report = dbutil.run(measure_routing(questions))

    assert report.n == 4
    assert report.accuracy == 0.25
    assert report.confusion == {
        "local": {"LOCAL": 1},
        "global": {"LOCAL": 1},
        "multihop": {"LOCAL": 2},
    }


def test_measure_routing_is_zero_on_no_questions(monkeypatch: pytest.MonkeyPatch) -> None:
    install_route(monkeypatch, Route.LOCAL)

    report = dbutil.run(measure_routing([]))

    assert report.n == 0 and report.accuracy == 0.0 and report.confusion == {}


def test_local_questions_reuse_the_harness_generation_at_the_stratum_size(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sizes: list[int] = []

    async def stub_build_questions(questions: list[str] | None, user: User) -> list[QA]:
        sizes.append(settings.eval_sample_questions)
        return [
            QA(question="what holds", expected="alpha holds"),
            QA(question="judge only", expected=None),
        ]

    monkeypatch.setattr(plans, "build_questions", stub_build_questions)

    questions = dbutil.run(plans.local_questions(User.system(), 3))

    assert sizes == [3]
    assert questions == [
        StudyQuestion(question="what holds", expected=("alpha holds",), stratum=Stratum.LOCAL)
    ]


def test_global_questions_ask_for_each_stored_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def pool(user: User, n: int) -> list[str]:
        assert n == 2
        return ["the compression theme", "the identity theme"]

    monkeypatch.setattr(plans, "summary_pool", pool)
    calls = install_structured(monkeypatch, "what is this area about?")

    questions = dbutil.run(plans.global_questions(User.system(), 2))

    assert [question.expected for question in questions] == [
        ("the compression theme",),
        ("the identity theme",),
    ]
    assert all(question.stratum is Stratum.GLOBAL for question in questions)
    assert [user for _, user in calls] == ["the compression theme", "the identity theme"]
    assert all("overview" in system for system, _ in calls)


def test_multihop_questions_pair_both_facts_as_expected_evidence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    a, b, c = _POOL[:3]
    edges = [
        GraphEdge(subject_id=a, object_id=b, statement="a funds b"),
        GraphEdge(subject_id=b, object_id=c, statement="b builds c"),
    ]

    async def stub_edges(user: User, cap: int) -> list[GraphEdge]:
        assert cap == 1 * 32
        return edges

    monkeypatch.setattr(plans, "graph_edges", stub_edges)
    calls = install_structured(monkeypatch, "how does a reach c?")

    questions = dbutil.run(plans.multihop_questions(User.system(), 1))

    assert questions == [
        StudyQuestion(
            question="how does a reach c?",
            expected=("a funds b", "b builds c"),
            stratum=Stratum.MULTIHOP,
        )
    ]
    [(system, user)] = calls
    assert "shared entity" in system
    assert user == "Fact one. a funds b\nFact two. b builds c"


@pytest.mark.parametrize("stratum", list(Stratum))
def test_stratum_questions_dispatches_to_its_generator(
    monkeypatch: pytest.MonkeyPatch, stratum: Stratum
) -> None:
    async def generator(user: User, n: int) -> list[StudyQuestion]:
        return [study_question(question=stratum.value, stratum=stratum)]

    monkeypatch.setattr(plans, "local_questions", generator)
    monkeypatch.setattr(plans, "global_questions", generator)
    monkeypatch.setattr(plans, "multihop_questions", generator)

    [question] = dbutil.run(stratum_questions(stratum, User.system(), 1))

    assert question.question == stratum.value


def test_summary_pool_reads_communities_then_raptor_summaries(migrated_db: None) -> None:
    async def probe() -> tuple[list[str], list[str]]:
        owner = await seedgraph.fresh_owner()
        user = User.private(owner)
        async with user as session:
            session.add(
                Community(
                    created_by=owner,
                    scopes=[owner],
                    label="theme",
                    summary="community summary",
                    embedding=None,
                )
            )
            content = EntityContent(name="Broad theme", type=ontology.RAPTOR_SUMMARY)
            session.add(content)
            await session.flush()
            session.add(
                EntityClaim(
                    content_id=content.id,
                    created_by=owner,
                    scopes=[owner],
                    attributes={"level": 1, "summary": "raptor summary"},
                )
            )
        return await summary_pool(user, 5), await summary_pool(user, 1)

    pooled, capped = dbutil.run(probe())

    assert pooled == ["community summary", "raptor summary"]
    assert capped == ["community summary"]


def test_graph_edges_reads_only_connected_visible_facts(migrated_db: None) -> None:
    async def probe() -> list[GraphEdge]:
        owner = await seedgraph.fresh_owner()
        user = User.private(owner)
        async with user as session:
            first = await seedgraph.add_entity(session, owner, "alpha")
            second = await seedgraph.add_entity(session, owner, "beta")
            await seedgraph.add_fact(session, owner, first, "alpha relates beta", object_id=second)
            await seedgraph.add_fact(session, owner, first, "alpha stands alone")
        return await graph_edges(user, 10)

    edges = dbutil.run(probe())

    assert [edge.statement for edge in edges] == ["alpha relates beta"]
    assert edges[0].object_id is not None


def stub_strata(monkeypatch: pytest.MonkeyPatch, per: dict[Stratum, int]) -> None:
    async def generate(stratum: Stratum, user: User, n: int) -> list[StudyQuestion]:
        expected = ("alpha holds",) if stratum is not Stratum.MULTIHOP else ("alpha holds", "far")
        return [
            StudyQuestion(question=f"{stratum} {index}", expected=expected, stratum=stratum)
            for index in range(per.get(stratum, 0))
        ]

    monkeypatch.setattr(plans, "stratum_questions", generate)


def test_run_plan_study_sweeps_every_arm_per_stratum_with_ablation_and_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    install_constant_recall(monkeypatch, plans, "alpha holds")
    install_route(monkeypatch, Route.LOCAL)
    stub_strata(monkeypatch, {Stratum.LOCAL: 1, Stratum.GLOBAL: 1, Stratum.MULTIHOP: 2})

    report = dbutil.run(run_plan_study(k=4, per_stratum=2, seed_floors=(0.4,)))

    assert isinstance(report, PlanStudyReport)
    assert [result.stratum for result in report.strata] == list(Stratum)
    for result in report.strata:
        assert [arm.arm for arm in result.arms] == [
            "local",
            "global",
            "multihop",
            "maximal",
            "routed",
        ]
        assert all(arm.hit_at_k == 1.0 for arm in result.arms)
    assert report.seeding is not None
    assert report.seeding.n == 2
    assert [arm.arm for arm in report.seeding.arms] == [
        "seeding=off",
        "seeding=exact",
        "seeding=exact+fuzzy",
        "seed_floor=0.4",
    ]
    assert report.routing is not None
    assert report.routing.n == 4
    assert report.routing.accuracy == 0.25
    assert report.gate is None


def test_run_plan_study_handles_empty_strata_and_skipped_ablation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub_strata(monkeypatch, {})

    empty = dbutil.run(run_plan_study(k=4))

    assert all(result.n == 0 and result.arms == [] for result in empty.strata)
    assert empty.seeding is None and empty.routing is None

    install_constant_recall(monkeypatch, plans, "alpha holds")
    install_route(monkeypatch, Route.MULTIHOP)
    stub_strata(monkeypatch, {Stratum.MULTIHOP: 1})

    unseeded = dbutil.run(run_plan_study(k=4, strata=(Stratum.MULTIHOP,), seeding=False))

    assert unseeded.seeding is None
    assert unseeded.routing is not None and unseeded.routing.accuracy == 1.0


def arm_score(judge: float | None = None) -> ArmScore:
    return ArmScore(
        arm="local", hit_at_k=1.0, ndcg_at_k=0.9, mrr=0.8, judge=judge, latency_p50_ms=12.3
    )


@pytest.mark.parametrize(
    ("report", "needles"),
    [
        (
            PlanStudyReport(
                k=4,
                strata=[StratumResult(stratum=Stratum.LOCAL, n=2, arms=[arm_score(judge=0.5)])],
                seeding=StratumResult(stratum=Stratum.MULTIHOP, n=2, arms=[arm_score()]),
                routing=RoutingReport(n=2, accuracy=0.5, confusion={"local": {"LOCAL": 2}}),
                gate=GateReport(
                    chunks=2,
                    accepted=1,
                    rejected=1,
                    rejected_with_facts=1,
                    facts_lost=2,
                    timed_out=0,
                ),
            ),
            [
                "plan study k=4",
                "stratum=local n=2",
                "local: hit@4=1.0",
                "judge=0.500",
                "seed ablation over multihop n=2",
                "routing accuracy=0.5 over n=2",
                "gate replay n=2",
            ],
        ),
        (
            PlanStudyReport(k=4, strata=[], seeding=None, routing=None),
            ["no strata"],
        ),
    ],
    ids=["filled", "empty"],
)
def test_render_shows_every_section_it_has(report: PlanStudyReport, needles: list[str]) -> None:
    rendered = report.render()

    assert all(needle in rendered for needle in needles)
