# Evidence-led performance engineering

- Type Concept
- part_of [Concept] Software engineering practice map
- depends_on [Concept] Observability and production diagnosis
- uses [Method] Representative workload profiling

Profile the representative workload before optimizing. Record workload identity, input size,
environment, baseline, call counts, elapsed time, resource use, variation, and the bottleneck the
evidence identifies. A microbenchmark can support a bounded mechanism claim. It cannot by itself
prove that a user-visible workflow improved.

Optimization is an engineering tradeoff. Compare before and after results while holding correctness
constant. Count memory, latency, throughput, cost, readability, and maintenance surface. The measured
benefit must earn any added complexity. Warmup, repetition, and variance matter, especially for
managed services where cold starts and network tails can dominate a median.

Profiling should change the refactoring plan. If most time is in one repeated database scan, index or
cache the shared work rather than rewriting unrelated orchestration. Measure the complete workflow
after the local change, since a faster inner operation matters only when it improves the path users
actually run.

Public references

- [Python profiling documentation](https://docs.python.org/3/library/profile.html)
- [Python timeit documentation](https://docs.python.org/3/library/timeit.html)
- [NVIDIA CUPTI overhead guidance](https://docs.nvidia.com/cupti/main/main.html)
- [PyTorch profiler](https://docs.pytorch.org/docs/stable/profiler)
