# Observability and production diagnosis

- Type Concept
- part_of [Concept] Software engineering practice map
- depends_on [Concept] Reliable delivery and recovery
- uses [Concept] Service-level objectives

Observability should let an operator explain an unfamiliar failure from production evidence. Logs,
metrics, and traces need shared request and deployment context so a symptom can be connected to the
exact code and artifact that produced it. High-cardinality dimensions are valuable when they help
slice behavior by customer, endpoint, region, version, or dependency without predicting every
failure in advance.

Service-level objectives connect telemetry to user experience. Alerts should be actionable and
should identify an owner, impact, and next diagnostic step. Dashboards without a recovery workflow
are not readiness. Production readiness combines service objectives, context-rich telemetry,
runbooks, ownership, load evidence, and recovery exercises.

Modular boundaries improve diagnosis when telemetry preserves those boundaries. A request trace can
then show which adapter, domain operation, or dependency consumed the time. The same boundaries make
failure paths easier to test because external dependencies can be replaced with controlled test
implementations. This is why maintainable design, testing, and observability reinforce each other.

Sources retained in pgAIZK include `Observability Engineering second edition publisher preview`,
document `019f7a54-11e4-7769-865a-55ea75d417ec`, and `Crivo JUDGMENT.md`, document
`019f7a9d-6672-7743-9a31-0a9cc80fcf1a`.

Public references

- [OpenTelemetry trace API](https://opentelemetry.io/docs/specs/otel/trace/api/)
- [OpenTelemetry semantic attributes](https://opentelemetry.io/docs/specs/semconv/general/attributes/)
- [Google SRE on evolving engagement](https://sre.google/sre-book/evolving-sre-engagement-model/)
- [Kubernetes production readiness review](https://github.com/kubernetes/enhancements/tree/master/keps/sig-architecture/1194-prod-readiness)
