import uuid

from hypothesis import strategies as st
from pydantic import UUID5, UUID7, UUID8


def uuid5() -> UUID5:
    """Generate one valid deterministic-identity UUID for a test case."""
    return uuid.uuid5(uuid.NAMESPACE_URL, str(uuid.uuid7()))


def uuid7() -> UUID7:
    """Generate one valid time-ordered record UUID for a test case."""
    return uuid.uuid7()


def uuid8() -> UUID8:
    """Generate one valid custom-identity UUID for a test case."""
    return uuid.uuid8()


def version7(value: int) -> UUID7:
    """Set RFC version and variant bits on an integer for UUID7 property tests."""
    return uuid.UUID(int=(value & ~(0xF << 76) & ~(0x3 << 62)) | (7 << 76) | (0x2 << 62))


def version8(value: int) -> UUID8:
    """Set RFC version and variant bits on an integer for UUID8 property tests."""
    return uuid.UUID(int=(value & ~(0xF << 76) & ~(0x3 << 62)) | (8 << 76) | (0x2 << 62))


uuid5s = st.uuids(version=5)
uuid7s = st.integers(min_value=0, max_value=2**128 - 1).map(version7)
uuid8s = st.integers(min_value=0, max_value=2**128 - 1).map(version8)
