#!/bin/sh
set -eu

revision="1bbe0d64d6017b346a5e6f7bf784ba7a3294662f"
source_root="${1:-}"
temporary_source=""
dist_root="$(mktemp -d /tmp/aizk-sqlalchemy-dist.XXXXXX)"

cleanup() {
    rm -rf "$dist_root"
    if [ -n "$temporary_source" ]; then
        rm -rf "$temporary_source"
    fi
}
trap cleanup EXIT HUP INT TERM

if [ -z "$source_root" ]; then
    temporary_source="$(mktemp -d /tmp/aizk-sqlalchemy-source.XXXXXX)"
    source_root="$temporary_source"
    git -C "$source_root" init --quiet
    git -C "$source_root" remote add origin https://github.com/Pedrexus/sqlalchemy.git
    git -C "$source_root" fetch --quiet --depth 1 origin "$revision"
    git -C "$source_root" checkout --quiet --detach FETCH_HEAD
fi

if [ "$(git -C "$source_root" rev-parse HEAD)" != "$revision" ]; then
    echo "SQLAlchemy source must be pinned to $revision" >&2
    exit 1
fi

DISABLE_SQLALCHEMY_CEXT=1 uv run --frozen pyproject-build \
    --wheel --outdir "$dist_root" "$source_root"
uv pip install --python .venv --reinstall --no-deps "$dist_root"/*.whl
uv run --no-sync python -c \
    "from sqlalchemy.dialects.postgresql import CreatePolicy, DropPolicy, Policy"
