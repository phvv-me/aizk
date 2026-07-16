# The read path

`recall()` is the retrieval entry point. It embeds the asker-aware query and always runs the
maximal plan, every lane on in facts-first order, with no query-time route classification. A
misrouted query loses community and RAPTOR evidence the reranker cannot recover, and the
zero-shot router measured 44% accuracy on the eval strata, so PostgreSQL ranks all visible
evidence in one statement and Python cuts the token budget after identity-aware reranking.

```mermaid
flowchart LR
    Q[question and asker] --> E[embedding and entity seeds]
    E --> S[one SQLAlchemy statement]
    S --> H[dense and lexical evidence]
    S --> P[current Area and Project catalogs]
    S --> G[graph walk, communities, RAPTOR]
    S --> M[working memory and profiles]
    H --> B[direct-source authority, merit rerank, and prefix budget cut]
    P --> B
    G --> B
    M --> B
    B --> C[one prompt-ready string]
```

## Typed recall statement

`build_recall_statement()` lays out one SQLAlchemy statement in execution order from mapped table
columns, vector distance operators, PostgreSQL text functions, CTEs, window functions, and unions.
There is no stored recall function and no handwritten runtime query string. SQLAlchemy owns
aliases, parameters, joins, and the compact result shape.

Hybrid source search means that a semantic vector lane and an exact-word lexical lane each collect
a bounded candidate pool. Reciprocal rank fusion combines their positions rather than trying to
compare incompatible raw scores. A result at rank `r` contributes `1 / (rrf_k + r)`, and a chunk
found by both lanes receives both contributions. The document join happens only after fusion,
which preserves index-friendly top-k scans.

A third source lane recognizes when the question contains a document's complete title. Its chunks
join the same candidate set and carry one direct-subject bit into final ordering. If one named title
is contained inside another, only the maximal title keeps direct authority. This prevents `JLPT N2`
from shadowing `JLPT N2 Window Weekly Plan`, while unrelated titles named together remain peers.
Directly named sources come before incidental evidence, and the cross-encoder orders each identity
group by answer quality. This rule does not route the query, disable a lane, or impose a fixed order
among evidence kinds.

Fact retrieval first asks the `FactContent` vector index for a bounded candidate set, then joins
only those rows to current `FactClaim` records. This keeps immutable embeddings separate from
scope and time while avoiding a full scan through the security-barrier view. The graph expansion
is a bounded recursive walk whose lateral adjacency probe uses the subject and object indexes for
each reached entity, and the community and RAPTOR lanes ride the same statement. The eval plan
study keeps the narrower historical shapes constructible for comparison, but production never
selects among them.

RAPTOR means Recursive Abstractive Processing for Tree-Organized Retrieval. It clusters related
memories, summarizes each cluster, and repeats that process into a hierarchy. Global recall reads
the root summaries as broad overviews while ordinary facts remain the grounded evidence below
them.

VectorChord provides the default vector and BM25 indexes. Native PostgreSQL full text and HNSW
remain the portable fallback. ParadeDB was not adopted because its current community deployment
documentation warns that production WAL recovery does not protect its indexes.

Chunk reads inherit visibility from their parent document, while chunk writes still require the
chunk's complete writable scope set. This preserves one authorization boundary for a document and
its spans and lets PostgreSQL use both VectorChord indexes. App connections enable VectorChord
prefilter because organization scopes are normally a strict and cheap filter.

## Perspective and evidence

The asking speaker label enriches query embedding without changing authorization. Speaker-bound
facts retain their author, role, epistemic kind, and perspective key in every hit. Objective world
facts share one consolidation partition. Experiences, observations, opinions, and preferences use
one partition per creator so two collaborators can disagree without overwriting each other.

Profiles rank by summary embedding rather than entity-name embedding. Source candidates expose the
complete configured chunk size to the cross-encoder, so a late Risks or Next actions section is not
silently truncated before scoring. After identity-aware merit ordering, a plain Python walk keeps
the longest prefix of candidates whose chars-per-token cost fits the budget. The MCP boundary first
builds a typed `RecallResult`. Each evidence object carries source text, public provenance, and
exact Logto scope objects with names and descriptions. A Jinja template then renders one string.
The public provenance classes are source excerpt, derived memory, and recent session memory.
Internal lane names and retrieval scores stay in `aizk eval trace` rather than leaking into the
prompt. Only selected facts receive access counter updates.

Area and Project catalogs are database-derived source candidates. The Project catalog includes
only documents whose explicit state is Active or Waiting and renders each state beside its Area,
so current work does not depend on an extractor interpreting tags, checkboxes, or old journal
prose.

## Authorization

Recall accepts a `User`, not caller-selected scopes. One request-scoped session binds that user's
Logto-derived read and write lattice before executing the query. Forced row level security remains
the complete visibility boundary for facts, chunks, profiles, working memory, and summaries.
