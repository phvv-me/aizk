import uuid
from contextlib import asynccontextmanager

import dbutil
import pytest

import aizk.admin as admin
from aizk.config import settings

DOC_A = uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
DOC_B = uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")


class Recorder:
    """An async call double recording its arguments and resolving to a fixed value."""

    def __init__(self, ret: object = None) -> None:
        self.ret = ret
        self.args: tuple[object, ...] = ()
        self.kwargs: dict[str, object] = {}

    async def __call__(self, *args: object, **kwargs: object) -> object:
        self.args = args
        self.kwargs = kwargs
        return self.ret


def test_system_is_the_configured_system_user() -> None:
    """An operator call acts as the system user by default, past row level security."""
    assert admin.system() == settings.system_user_id


@pytest.mark.parametrize(
    ("fn", "delegate", "ret", "expected"),
    [
        ("rebuild", "build_graph", (3, 5), (3, 5)),
        ("decay", "decay", 7, 7),
        ("reembed", "reembed", 9, 9),
        ("raptor", "build_raptor", 4, 4),
    ],
)
def test_maintenance_op_defaults_to_the_system_user(
    monkeypatch: pytest.MonkeyPatch, fn: str, delegate: str, ret: object, expected: object
) -> None:
    """A maintenance op with no explicit user drives its graph delegate as the system one."""
    recorder = Recorder(ret=ret)
    monkeypatch.setattr(admin.graph, delegate, recorder)

    out = dbutil.run(getattr(admin, fn)())

    assert out == expected
    assert recorder.kwargs["user_id"] == settings.system_user_id


def test_maintenance_op_honors_an_explicit_user(monkeypatch: pytest.MonkeyPatch) -> None:
    """A named user overrides the system default, the scoped-view seam for a tenant op."""
    recorder = Recorder(ret=0)
    monkeypatch.setattr(admin.graph, "decay", recorder)
    who = uuid.uuid4()

    dbutil.run(admin.decay(half_life_days=30.0, user_id=who))

    assert recorder.kwargs["user_id"] == who
    assert recorder.kwargs["half_life_days"] == 30.0


def test_forget_ranks_documents_by_the_query_then_retracts_their_claims(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Forget embeds the query, ranks the nearest notes, and retracts their derived claims.

    The provenance chain the operator's erasure runs: one embed, a nearest-document rank under the
    user's own RLS, and a `forget_from_documents` over exactly those ids, the titles reported
    back so the operator sees what left before committing.
    """

    class FakeEmbedder:
        async def embed(self, texts: list[str], mode: str) -> list[list[float]]:
            return [[0.1, 0.2]]

    class Result:
        def __init__(self, values: list[object]) -> None:
            self._values = values

        def scalars(self) -> list[object]:
            return self._values

    class FakeSession:
        def __init__(self) -> None:
            self.calls = 0

        async def execute(self, statement: object) -> Result:
            self.calls += 1
            # first execute ranks document ids, second reads their titles
            return Result([DOC_A, DOC_B]) if self.calls == 1 else Result(["Note A", None])

    @asynccontextmanager
    async def fake_acting_as(user_id: uuid.UUID):
        fake = FakeSession()
        async with dbutil.use_session(fake):
            yield fake

    async def fake_forget(doc_ids: list[uuid.UUID]) -> list[uuid.UUID]:
        return doc_ids  # every named document contributed one live claim

    monkeypatch.setattr(admin, "Embedder", FakeEmbedder)
    monkeypatch.setattr(admin, "acting_as", fake_acting_as)
    monkeypatch.setattr(admin.FactClaim, "forget_from_documents", fake_forget)

    result = dbutil.run(admin.forget("a wrong note", k=8))

    assert result.claims == 2  # both ranked documents' claims retracted
    assert result.documents == ["Note A"]  # the null title dropped, the real one kept


def test_link_user_binds_a_subject_and_is_idempotent(migrated_db: None) -> None:
    """Linking an OIDC subject mints a user, and a second link over the same subject reuses it."""

    async def run() -> tuple[uuid.UUID, uuid.UUID]:
        await dbutil.reset_db()
        await dbutil.seed_user(settings.system_user_id)
        first = await admin.link_user("gh|7", "Ada")
        again = await admin.link_user("gh|7", "ignored")
        return first.id, again.id

    first_id, again_id = dbutil.run(run())
    assert first_id == again_id  # idempotent over the same subject


def test_benchmark_refuses_when_the_engine_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """The benchmark op is gated off by default, an explicit opt-in the datasets need."""
    monkeypatch.setattr(settings, "benchmarks_enabled", False)
    with pytest.raises(ValueError, match="benchmarks are off"):
        dbutil.run(admin.benchmark("evermembench", "x.jsonl"))


def test_benchmark_rejects_an_unknown_dataset_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the engine on, an unknown benchmark name is a fail-fast, not a silent empty sweep."""
    monkeypatch.setattr(settings, "benchmarks_enabled", True)
    with pytest.raises(ValueError, match="unknown benchmark"):
        dbutil.run(admin.benchmark("nope", "x.jsonl"))


def test_add_member_runs_against_the_live_schema(migrated_db: None) -> None:
    """`add_member` mints a real membership on a Logto-mirrored group under one system session.

    Groups come only from the identity provider, so the group is seeded as its mirror would be,
    then the actual `add_member` commit runs so the row-level-security grants land for real, and
    the roster read back proves the member joined.
    """

    async def run() -> list[dict]:
        await dbutil.reset_db()
        await dbutil.seed_user(settings.system_user_id)
        member = await dbutil.seed_user(uuid.uuid4())
        await dbutil.seed_group(uuid.uuid4(), name="team", public=True)
        await admin.add_member(str(member), "team", role="editor")
        return await admin.list_groups()

    roster = dbutil.run(run())

    team = next(row for row in roster if row["name"] == "team")
    assert team["public"] is True and team["members"] == 1
    assert team["members"] >= 1  # the creator-admin plus the added editor
