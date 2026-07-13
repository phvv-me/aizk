# Archive

Parked explorations kept for reference, never wired into the build.

- `ts/` is the 2026-07 TypeScript port of the whole engine (SvelteKit + drizzle). It reached
  verified full parity with the Python server, recall and rerank byte-identical, extraction
  proven on twin databases, all graph passes, Logto auth, and a pgqueuer-compatible worker.
  We stayed with Python because the engine's leverage lives in the eval harness and research
  iteration speed, the twin recall programs doubled maintenance, and drizzle-kit cannot diff
  functions, triggers, extensions, or enum values. The port's verification did catch real
  bugs that were fixed on the live side, and its findings are recorded in the session notes.
