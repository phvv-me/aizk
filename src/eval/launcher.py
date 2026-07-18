import os
import sys

_ISOLATED = frozenset({"extraction", "groupmem", "scale"})


def main() -> None:
    """Launch isolated commands against the evaluation database."""
    environment = os.environ.copy()
    if len(sys.argv) > 1 and sys.argv[1] in _ISOLATED:
        environment["AIZK_DB_NAME"] = environment.get("AIZK_EVAL_DB_NAME", "aizk_eval")
        environment.pop("AIZK_DATABASE_URL", None)
        environment.pop("AIZK_ADMIN_DATABASE_URL", None)
    os.execvpe(
        sys.executable,
        [sys.executable, "-m", "eval.cli", *sys.argv[1:]],
        environment,
    )


if __name__ == "__main__":
    main()
