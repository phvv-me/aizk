# Testing and code review

- Type Concept
- part_of [Concept] Software engineering practice map
- depends_on [Concept] Maintainable design and modular boundaries
- uses [Concept] Continuous integration

Automated tests should protect important behavior and failure scenarios. A test strategy is
proportional to risk. It considers isolation, failure-scenario coverage, flaky-test rate, and how
quickly continuous integration reports a regression. Coverage is one signal, not a substitute for
testing meaningful behavior. A flaky test weakens the whole feedback system because developers stop
trusting failures.

Code review is an independent engineering judgment. Reviewers should examine design, functionality,
complexity, tests, naming, comments, style, and documentation. The best reviewer understands the
affected code and can respond in a reasonable time. A change should be small enough for a reviewer
to understand its purpose and consequences. Protected main branches and reviewed merges turn that
judgment into a reliable gate.

Continuous integration should build every commit in a reproducible environment and fail quickly on
meaningful defects. Keep the main branch healthy. A green build is evidence only when it tests the
same resolved dependencies and artifact that will be deployed. When a static analysis warning seems
wrong, first determine whether the code or rule can be improved. Suppression is the last resort and
must explain why the warning is a genuine tool limitation.

Sources retained in pgAIZK include Google's `review`, document
`019f7a28-94d9-77ab-8f82-481b326c7c20`, and Pedro Valois's `Meteng rule taxonomy expansion`,
document `019f83e0-6778-7077-a0d9-8a9b6fdf0136`.

Public references

- [Google Engineering Practices for code review](https://google.github.io/eng-practices/review/)
- [Google guidance for small changes](https://google.github.io/eng-practices/review/developer/small-cls.html)
- [Google Software Engineering on testing](https://abseil.io/resources/swe-book/html/ch14.html)
