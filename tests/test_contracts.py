import tomllib
from pathlib import Path

_ROOT = Path(__file__).parents[1]

_SQL_COMPOSING = {
    "admin",
    "artifacts",
    "background",
    "backup",
    "config",
    "export",
    "extract",
    "graph",
    "ontology",
    "ops",
    "retrieval",
    "store",
}


def packages() -> set[str]:
    """List every top-level aizk module and package present in the source tree."""
    return {
        path.stem if path.suffix == ".py" else path.name
        for path in (_ROOT / "src" / "aizk").iterdir()
        if (path.suffix == ".py" and path.stem != "__init__") or (path / "__init__.py").is_file()
    }


def test_sql_forbidden_contract_splits_every_package() -> None:
    """The SQL contract's source list plus the documented exceptions covers every package.

    The layers contract is exhaustive natively; this keeps the forbidden contract from
    silently accepting a new package that starts composing SQL outside the store.
    """
    contracts = tomllib.loads((_ROOT / "pyproject.toml").read_text())["tool"]["importlinter"][
        "contracts"
    ]
    forbidden = next(contract for contract in contracts if contract["type"] == "forbidden")
    sources = {name.removeprefix("aizk.") for name in forbidden["source_modules"]}
    assert sources | _SQL_COMPOSING == packages()
    assert not sources & _SQL_COMPOSING
