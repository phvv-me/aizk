# Aizk testing

The suite protects behavior at the narrowest useful boundary. A test should explain one cohesive
contract and may assert every observable part of that contract. Splitting each field or branch
into a separate function makes the suite longer without making failures clearer.

## Choosing a test style

Use Hypothesis when the behavior is an invariant over a broad input space. Good examples include
scope lattices, temporal ranges, deterministic identities, ordering, packing, parsing, and wire
schema bounds. Keep strategies close to the domain and constrain invalid combinations in the
strategy instead of discarding most examples with `assume`.

Use `pytest.mark.parametrize` for a small finite partition such as supported enum members, exact
error classes, protocol variants, or boundary cases with distinct expected results. Combine
repetitive functions when setup, action, and assertion describe the same contract.

Use Polyfactory when a valid Pydantic or SQLModel instance is only test setup. Override fields that
matter to the behavior under test and let the factory supply incidental values. Prefer a domain
strategy over a factory when generated values themselves drive the property.

Use fakes for model, queue, clock, and database seams when their behavior participates in the
test. Use mocks only to verify a narrow call boundary. HTTP and subprocess tests should replace
the transport or process boundary rather than patching several internal helpers.

## Database isolation

Every pytest process receives its own `aizk_test_<pid>` database. Session setup creates and
migrates that database when PostgreSQL is reachable. Session teardown drops it even after test
failures. Collection remains read-only. Parallel local runs and agent-owned focused runs therefore
cannot erase one another's state.

Database properties use fewer examples than pure properties because every example may open a
transaction. Keep each example rollback-safe and never depend on execution order.

## Coverage and commands

Coverage is a backstop for missing behavior, not a reason to preserve repetitive tests. The gate
requires 100 percent statement and branch coverage across `aizk` and `eval`.

Run focused feedback through `chefe` while editing.

```sh
chefe run -- pytest tests/path/test_file.py --no-cov
chefe run -- ruff check tests/path/test_file.py
```

Run the complete serialized gate before handoff.

```sh
chefe run test-aizk-cov
chefe run typecheck-aizk
```
