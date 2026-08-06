import sys
from collections.abc import Callable
from typing import TYPE_CHECKING, cast

from loguru import logger
from opentelemetry import trace
from opentelemetry.trace import format_span_id, format_trace_id

from .settings import DatabaseBackend, Settings

if TYPE_CHECKING:
    # loguru ships `Record` in its stub only, so the runtime module has no such name.
    from loguru import Record

# Shared process configuration
settings = cast("Callable[[], Settings]", Settings)()


def correlate_trace(record: Record) -> None:
    """Add the active trace and span ids to one log record.

    Serialized records carry these under `extra`, which is what lets Grafana turn a log line
    in Loki into a link to the same request's trace in Tempo. Outside a recorded span both
    stay empty rather than absent, so the shape of a record never depends on tracing.
    """
    span_context = trace.get_current_span().get_span_context()
    recorded = span_context.is_valid
    record["extra"]["trace_id"] = format_trace_id(span_context.trace_id) if recorded else ""
    record["extra"]["span_id"] = format_span_id(span_context.span_id) if recorded else ""


def configure_logging(level: str, serialize: bool = False) -> None:
    """Point aizk's single stderr log sink at `level`, or silence the library when it is
    empty."""
    if level:
        logger.enable("aizk")
        logger.remove()
        logger.configure(patcher=correlate_trace)
        logger.add(sys.stderr, level=level, serialize=serialize)
    else:
        logger.disable("aizk")


configure_logging(settings.log_level, settings.log_json)


__all__ = [
    "DatabaseBackend",
    "Settings",
    "configure_logging",
    "correlate_trace",
    "settings",
]
