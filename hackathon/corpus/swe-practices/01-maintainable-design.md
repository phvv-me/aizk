# Maintainable design and modular boundaries

- Type Concept
- part_of [Concept] Software engineering practice map
- uses [Pattern] Deep module
- depends_on [Concept] Explicit dependencies

Maintainability is the ability to change software safely without understanding the whole system at
once. A useful module hides substantial implementation complexity behind a small, stable interface.
Splitting code into ever smaller units is not a goal by itself. The boundary earns its existence
when it reduces knowledge that callers must carry.

Keep policy and domain decisions separate from infrastructure details. Put databases, hosted APIs,
and framework adapters behind narrow contracts so business logic can be tested without those
systems. Prefer cohesive responsibilities and explicit dependencies. Interfaces should contain only
operations their implementers need. New behavior should usually arrive through a new implementation
of a stable contract instead of another branch in a central conditional.

Architecture rules must follow the architecture the project actually declares. Clean Architecture
does not mean imposing one universal folder layout. SOLID principles are design prompts, not scores.
Pattern counts and tiny-function counts are poor quality measures. Refactoring is justified when it
preserves external behavior and makes a likely change easier, safer, or clearer.

Public references

- [A Philosophy of Software Design extract](https://web.stanford.edu/~ouster/cgi-bin/aposd2ndEdExtract.pdf)
- [Building Maintainable Software](https://www.oreilly.com/library/view/building-maintainable-software/9781491955987/)
- [Martin Fowler on refactoring](https://refactoring.com/)
- [Design Patterns ACM record](https://dl.acm.org/doi/10.5555/186897)
