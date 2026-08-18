from contextlib import AbstractContextManager

from opentelemetry import trace
from opentelemetry.trace import Span


def span(name: str) -> AbstractContextManager[Span]:
    """Open one child span through AIZK's configured OpenTelemetry provider."""
    return trace.get_tracer("aizk").start_as_current_span(name)
