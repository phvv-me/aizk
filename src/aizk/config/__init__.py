import sys

from loguru import logger

from .settings import Settings

# the one runtime configuration every module reads directly, built once from the environment at
# import time. Every AIZK_-prefixed env var is fixed for the process lifetime, so re-reading it per
# call would only reload the same values. A field can still change in-process through a direct
# `setattr`, such as the self-improve pass's config flip, since every module holds this same
# singleton rather than a copy.
settings = Settings()


def configure_logging(level: str) -> None:
    """Point aizk's single stderr log sink at `level`, or silence the library when it is empty.

    Run once at import so no caller has to remember to configure logging before using the package.
    An empty level disables the logger outright rather than filtering it, the quiet default for an
    embedding library, while any other level enables it and points a single stderr sink at it.

    level: minimum log level for the stderr sink, empty to disable aizk's logging entirely.
    """
    if level:
        logger.enable("aizk")
        logger.remove()
        logger.add(sys.stderr, level=level)
    else:
        logger.disable("aizk")


configure_logging(settings.log_level)


__all__ = ["configure_logging", "settings"]
