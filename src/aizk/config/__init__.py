import sys

from loguru import logger

from .settings import Settings

# Shared process configuration
settings = Settings()


def configure_logging(level: str) -> None:
    """Point aizk's single stderr log sink at `level`, or silence the library when it is
    empty."""
    if level:
        logger.enable("aizk")
        logger.remove()
        logger.add(sys.stderr, level=level)
    else:
        logger.disable("aizk")


configure_logging(settings.log_level)


__all__ = ["configure_logging", "settings"]
