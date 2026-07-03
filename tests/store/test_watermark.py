import asyncio
import socket
import uuid
from urllib.parse import urlsplit

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule
from sqlalchemy import delete

from aizk.config import Settings
from aizk.store import Watermark, acting_as


def db_up() -> bool:
    """Whether the configured Postgres DSN accepts a TCP connection, the DB-test gate."""
    parts = urlsplit(Settings().database_url)
    if parts.hostname is None or parts.port is None:
        return False
    try:
        with socket.create_connection((parts.hostname, parts.port), timeout=0.5):
            return True
    except OSError:
        return False


DB_UP = db_up()

# the watermark keys the machine drives, a small kind and ref grid namespaced per run so a fresh
# read is genuinely zero and one machine never sees another's leftover rows
kinds = st.sampled_from([Watermark.Kind.entity_dirty, Watermark.Kind.fact_count])
refs = st.sampled_from(["one", "two"])
# payload keys stay over printable ascii since Postgres jsonb text rejects a NUL code point, a
# third-party storage limit rather than anything the watermark writer owns
payload_keys = st.text(alphabet="abcdefghijklmnopqrstuvwxyz", min_size=1, max_size=4)
payloads = st.dictionaries(payload_keys, st.integers(-9, 9), max_size=3)


class WatermarkMachine(RuleBasedStateMachine):
    """The per-principal counter's lifecycle, the bump-accumulates and set-overwrites invariant.

    A Python reference model mirrors what each watermark op must leave behind, and after every step
    the real read and read_payload are asserted equal to it, so a sequence of bumps and sets over a
    grid of keys proves the upsert accumulates, the absolute write overwrites, and an untouched key
    reads zero with an empty payload.
    """

    def __init__(self) -> None:
        super().__init__()
        self.owner = Settings().system_principal_id
        self.run_id = uuid.uuid4().hex
        self.model: dict[tuple[Watermark.Kind, str], tuple[int, dict]] = {}

    def namespaced(self, ref: str) -> str:
        """Scope a ref to this machine run so leftover rows never bleed across instances.

        ref: the short grid ref the rule drew.
        """
        return f"{self.run_id}:{ref}"

    @initialize()
    def clear(self) -> None:
        """Start from a clean grid, deleting any row a prior crashed run left for this run."""
        asyncio.run(self.purge())

    @rule(kind=kinds, ref=refs, by=st.integers(-5, 5))
    def bump(self, kind: Watermark.Kind, ref: str, by: int) -> None:
        """Increment a counter and assert the returned value matches the accumulating model.

        kind: discriminator naming the counter.
        ref: subject the counter is keyed to.
        by: amount to add.
        """
        key = (kind, self.namespaced(ref))
        returned = asyncio.run(self.do_bump(*key, by))
        counter, payload = self.model.get(key, (0, {}))
        self.model[key] = (counter + by, payload)
        assert returned == counter + by

    @rule(kind=kinds, ref=refs, counter=st.integers(-50, 50), payload=payloads)
    def set_value(self, kind: Watermark.Kind, ref: str, counter: int, payload: dict) -> None:
        """Overwrite a counter and payload outright, the high-water and scorecard writer.

        kind: discriminator naming the counter.
        ref: subject the counter is keyed to.
        counter: the absolute value to store.
        payload: the structured detail to store.
        """
        key = (kind, self.namespaced(ref))
        asyncio.run(self.do_set(*key, counter, payload))
        self.model[key] = (counter, payload)

    @invariant()
    def every_key_reads_back_the_model(self) -> None:
        """Every grid key reads the modeled counter and payload, an untouched one reads zero."""
        for kind in (Watermark.Kind.entity_dirty, Watermark.Kind.fact_count):
            for ref in ("one", "two"):
                key = (kind, self.namespaced(ref))
                counter, payload = asyncio.run(self.do_read(*key))
                assert (counter, payload) == self.model.get(key, (0, {}))

    def teardown(self) -> None:
        """Delete every row this run created so the grid stays isolated across machines."""
        asyncio.run(self.purge())

    async def do_bump(self, kind: Watermark.Kind, ref: str, by: int) -> int:
        """Run the real bump under the system principal and return the new counter."""
        async with acting_as(self.owner) as session:
            return await Watermark.bump(session, self.owner, kind, ref=ref, by=by)

    async def do_set(self, kind: Watermark.Kind, ref: str, counter: int, payload: dict) -> None:
        """Run the real absolute set under the system principal."""
        async with acting_as(self.owner) as session:
            await Watermark.set_value(
                session, self.owner, kind, counter=counter, payload=payload, ref=ref
            )

    async def do_read(self, kind: Watermark.Kind, ref: str) -> tuple[int, dict]:
        """Read the real counter and payload back under the system principal."""
        async with acting_as(self.owner) as session:
            counter = await Watermark.read(session, self.owner, kind, ref=ref)
            payload = await Watermark.read_payload(session, self.owner, kind, ref=ref)
            return counter, payload

    async def purge(self) -> None:
        """Delete this run's watermark rows under the system principal."""
        async with acting_as(self.owner) as session:
            await session.execute(
                delete(Watermark)
                .where(Watermark.owner_id == self.owner)
                .where(Watermark.ref.startswith(f"{self.run_id}:"))
            )


WatermarkMachine.TestCase.settings = settings(
    max_examples=10,
    stateful_step_count=12,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
TestWatermarkMachine = pytest.mark.skipif(not DB_UP, reason="aizk postgres not reachable")(
    WatermarkMachine.TestCase
)
