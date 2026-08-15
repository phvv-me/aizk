# Screenshot shot list

Every capture should be cropped tightly enough to read at Devpost thumbnail size. Use only the
public demonstration account and corpus.

The default submission thumbnail is generated at `../thumbnail.png` from the canonical 3 to 2
brand source. Replace it only when a stronger final product capture is ready.

1. A grounded `find` result with source excerpt, document handle, and privacy receipt
2. CockroachDB tables for documents, chunks, temporal claims, scoped vectors, and queue state
3. EXPLAIN ANALYZE showing C-SPANN on the scoped vector projection
4. The completed worker Lambda invocation with duration and no error
5. Lambda logs, the gross-cost budget, and the EventBridge recovery schedule
6. ccloud cluster inspection with no credential or connection string visible
7. The architecture diagram from [ARCHITECTURE.md](../ARCHITECTURE.md)
8. Optional Managed MCP read-only schema or plan inspection

Do not capture another production deployment, private notes, local shell history, SSM values,
Logto identifiers, or OpenRouter keys.
