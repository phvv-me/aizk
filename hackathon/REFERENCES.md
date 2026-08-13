# Public corpus reference review

Reviewed on August 13, 2026.

The six authored software engineering notes cite nineteen public references. Seventeen targets
returned a successful automated response. The ACM Digital Library and O'Reilly catalog pages
refused automated requests with HTTP 403 responses. Their citations remain bibliographic links and
no content from either page is preserved in crAIZK.

The corpus stores only original synthesis notes and outbound citations. It does not copy, upload,
or redistribute the linked pages, books, extracts, specifications, or documentation.

## Maintainable design

- [A Philosophy of Software Design extract](https://web.stanford.edu/~ouster/cgi-bin/aposd2ndEdExtract.pdf)
- [Building Maintainable Software](https://www.oreilly.com/library/view/building-maintainable-software/9781491955987/)
- [Martin Fowler on refactoring](https://refactoring.com/)
- [Design Patterns ACM record](https://dl.acm.org/doi/10.5555/186897)

Terms represented include deep modules, explicit dependencies, cohesive responsibilities,
refactoring, SOLID guidance, and design patterns.

## Testing and review

- [Google Engineering Practices for code review](https://google.github.io/eng-practices/review/)
- [Google guidance for small changes](https://google.github.io/eng-practices/review/developer/small-cls.html)
- [Google Software Engineering on testing](https://abseil.io/resources/swe-book/html/ch14.html)

Terms represented include risk-proportional testing, failure scenarios, flaky tests, small changes,
independent review, continuous integration, and reproducible builds.

## Reliable delivery

- [Google SRE guidance for canarying releases](https://sre.google/workbook/canarying-releases/)
- [AWS guidance for timeouts, retries, and jitter](https://aws.amazon.com/builders-library/timeouts-retries-and-backoff-with-jitter/)
- [Martin Fowler on parallel change](https://martinfowler.com/bliki/ParallelChange.html)
- [SLSA build provenance](https://slsa.dev/spec/v1.2/provenance)

Terms represented include canary releases, bounded retries, backoff and jitter, parallel change,
expand and contract migrations, recovery exercises, and build provenance.

## Observability

- [OpenTelemetry trace API](https://opentelemetry.io/docs/specs/otel/trace/api/)
- [OpenTelemetry semantic attributes](https://opentelemetry.io/docs/specs/semconv/general/attributes/)
- [Google SRE on evolving engagement](https://sre.google/sre-book/evolving-sre-engagement-model/)
- [Kubernetes production readiness review](https://github.com/kubernetes/enhancements/tree/master/keps/sig-architecture/1194-prod-readiness)

Terms represented include traces, semantic attributes, service-level objectives, actionable alerts,
production readiness, runbooks, and recovery workflows.

## Performance engineering

- [Python profiling documentation](https://docs.python.org/3/library/profile.html)
- [Python timeit documentation](https://docs.python.org/3/library/timeit.html)
- [NVIDIA CUPTI overhead guidance](https://docs.nvidia.com/cupti/main/main.html)
- [PyTorch profiler](https://docs.pytorch.org/docs/stable/profiler)

Terms represented include representative workloads, profiling overhead, warmup, variance,
microbenchmarks, end-to-end measurements, and optimization tradeoffs.
