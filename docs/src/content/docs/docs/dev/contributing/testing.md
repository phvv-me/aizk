---
title: "Testing"
description: "The testing philosophy, the fixtures, and the coverage gate."
---

This page assumes you have a working environment from
[Development setup](/docs/dev/contributing/setup/). Everything below runs through `chefe run`, and
the short version of the philosophy is in `tests/README.md`, which this page expands.

## One contract per test

The suite protects behavior at the narrowest useful boundary. A test should describe one cohesive
contract, and it may assert every observable part of that contract in one function. Splitting each
field and each branch into its own test makes the suite longer without making a failure any easier
to read, so combine functions whose setup, action, and assertion are describing the same thing.

Which tool you reach for follows from what the behavior actually is.

| The behavior is | Use | Because |
|---|---|---|
| an invariant over a broad input space | Hypothesis | scope lattices, temporal ranges, deterministic ids, ordering, packing, parsing, wire bounds |
| a small finite partition | `pytest.mark.parametrize` | enum members, exact error classes, protocol variants, boundary cases |
| incidental setup that has to be valid | Polyfactory | a Pydantic or SQLModel instance whose values do not drive the property |
| a seam whose behavior participates | a fake | models, queues, clocks, the database |
| a narrow call boundary | a mock | and only then |

Keep Hypothesis strategies close to the domain and constrain invalid combinations inside the
strategy rather than throwing most examples away with `assume`. Reach for a domain strategy instead
of a factory whenever the generated values are the thing under test. For HTTP and subprocess work,
replace the transport or the process boundary rather than patching a handful of internal helpers,
which is what `pytest-subprocess` and a stubbed httpx transport are there for.

The registered Hypothesis profile in `tests/conftest.py` runs 60 examples with a 2 second deadline
and suppresses the function-scoped fixture health check. Database properties lower their own
example count locally, because every example may open a transaction. Keep each example
rollback-safe and never depend on execution order, which `pytest-randomly` will find out about
sooner rather than later.

## Fakes, not a live service

The suite is hermetic above the database seam. An autouse fixture points the embedder, reranker,
gate, and extraction model at in-process doubles from `tests/doubles.py`, so ontology bootstrap,
recall, and extraction all resolve to `RecordingEmbedder`, `NeutralReranker`, `NeutralGate`, and
`FakeLLM` instead of reaching a GPU. `tests/a_env.py` runs before aizk is even imported and blanks
any ambient rerank endpoint from a developer's `.env`, so a live sidecar on the machine cannot
quietly reroute a test. A test that genuinely needs real client construction opts out through the
`real_services` marker.

## One database per pytest process

The database is not faked. Each pytest process gets its own, named from its own pid.

```text
  tests/a_env.py            AIZK_DB_NAME = aizk_test_<pid>     (before aizk imports)
        │
        ▼
  session setup   ──▶  DROP DATABASE IF EXISTS ... WITH (FORCE)
                       CREATE DATABASE
                       ops.setup()   migrate, queue schema, grants, ontology
        │
        ▼
  the tests run as aizk_app, a NOBYPASSRLS role
        │
        ▼
  session teardown ──▶ DROP DATABASE ... WITH (FORCE)   even after failures
```

Two consequences are worth knowing. Parallel local runs and a focused run an agent starts on the
side cannot erase each other's state, which is why `-n 4` is safe. And collection stays read-only,
so listing tests never touches a database. If PostgreSQL is not reachable the fixture yields
without creating anything and the database tests skip rather than fail.

## The coverage gate

Coverage is a backstop against behavior nobody tested, not a reason to keep a repetitive test. The
gate is 100 percent statement **and** branch coverage across both `aizk` and `eval`, set by
`fail_under = 100` with `branch = true` in `pyproject.toml`. Migrations are the only omission.

The gate runs in two passes and the reason is a real limitation rather than a workaround. The
default `sysmon` coverage core cannot emit one `async with` enter arc inside the retry loop in
`graph/build.py`, so `chefe run test-aizk-cov` runs the parallel suite first with the threshold
disabled, then re-runs that single test under `COVERAGE_CORE=pytrace` with `--cov-append`. The
union of the two passes is what has to reach 100.

Four markers shape what runs. The default `addopts` deselect `integration` and `benchmark`, so an
ordinary run is the fast hermetic suite. `artifact_stack` narrows the integration suite to the full
file path, and `real_services` opts a test out of the model-lane stubbing.

## The integration target

One suite runs against the real services rather than doubles, and it runs inside the Compose
network so the service names resolve the way they do in production.

```sh
chefe run test-aizk-artifact-stack
```

That task starts `db`, `objects`, `clamav`, `docling`, `vllm-emb`, `vllm-rerank`, `vllm-llm`, and
`gliner` with `--no-recreate` and a 900 second readiness wait, then runs the `artifact-integration`
Compose service, which is built from the `integration-test` target of `src/deploy/Dockerfile` and
executes `tests/integration/test_artifact_stack.py`. The whole run is bounded by a 2,700 second
timeout and a trap removes the container on any exit, because an integration job that hangs is
worse than one that fails.

What it proves is the part doubles cannot. Real bytes go through the real malware scan, including
an EICAR sample that has to be rejected, real PDFs go through Docling, real vectors come from the
real embedder, and PgQueuer carries the work. [Deployment topology](/docs/dev/run/topology/)
describes the same services in their production arrangement.

## What CI runs

CI is deliberately not a different thing. It installs chefe, runs `chefe install`, and then runs
lint, the import contracts, typecheck, and `chefe run test` against a real PostgreSQL service
container using the same VectorChord image on the same port 5433, with the same restricted
`aizk_app` role bootstrapped over the wire because a service container cannot mount
`initdb/roles.sh`. macOS runners carry no service containers, so the suite is a Linux job for now.

## Next

<div class="not-content">

- [Style and typing](/docs/dev/contributing/style/) covers the linters and the three type checkers.
- [Row level security](/docs/dev/store/rls/) explains what the `aizk_app` role is proving.
- [How we evaluate](/docs/dev/eval/approach/) covers benchmarks, which are a separate thing from tests.

</div>
