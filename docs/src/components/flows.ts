import type { Edge, Node } from '@xyflow/svelte';

/** One clickable box. The sentence and the link are what make the diagram worth clicking. */
export type Detail = { title: string; body: string; href: string };

export type Flow = { nodes: Node[]; edges: Edge[]; detail: Record<string, Detail>; initial: string };

const box = (id: string, label: string, x: number, y: number): Node => ({
  id,
  position: { x, y },
  data: { label },
  width: 180,
  height: 52,
  class: 'aizk-node',
});

const link = (id: string, source: string, target: string, label?: string): Edge => ({
  id,
  source,
  target,
  label,
  animated: id.startsWith('a'),
});

const system: Flow = {
  initial: 'store',
  nodes: [
    box('client', 'MCP client', 220, 0),
    box('logto', 'Logto identity', 480, 0),
    box('write', 'write path', 0, 110),
    box('read', 'read path', 440, 110),
    box('graph', 'gate → extract → ground', 0, 220),
    box('store', 'PostgreSQL', 220, 330),
    box('passes', 'autonomous passes', 0, 440),
    box('evidence', 'evidence string', 440, 440),
  ],
  edges: [
    link('a1', 'client', 'write', 'remember'),
    link('a2', 'client', 'read', 'recall'),
    link('e1', 'write', 'graph'),
    link('e2', 'graph', 'store'),
    link('e3', 'store', 'passes'),
    link('e4', 'passes', 'store'),
    link('e5', 'store', 'read'),
    link('a3', 'read', 'evidence'),
    link('e6', 'logto', 'store', 'scopes'),
  ],
  detail: {
    client: {
      title: 'The MCP client',
      body: 'Claude Code, Codex, OpenCode or the web app. It calls four tools over one OAuth-protected endpoint and needs no aizk-specific integration code.',
      href: '/docs/dev/interfaces/mcp/',
    },
    logto: {
      title: 'Logto',
      body: 'The only authority on who a caller is and which organizations they belong to. aizk mirrors none of it and derives a stable identity from the verified token instead.',
      href: '/docs/dev/identity/logto/',
    },
    write: {
      title: 'The write path',
      body: 'Text and files become normalized Markdown, then chunks, then vectors. A file is scanned before it is stored and converted only after it is accepted.',
      href: '/docs/dev/write/intake/',
    },
    graph: {
      title: 'Graph projection',
      body: 'A relevance gate drops chunks with nothing to say. What survives goes through one extraction call, gets grounded against a quote from the text, then consolidated against what is already known.',
      href: '/docs/dev/write/extraction/',
    },
    store: {
      title: 'One PostgreSQL database',
      body: 'Documents, chunks, artifacts, entities, facts, communities, profiles and the job queue all live here, under forced row level security keyed on a set of scopes.',
      href: '/docs/dev/store/data-model/',
    },
    passes: {
      title: 'Autonomous passes',
      body: 'Scheduled jobs detect communities, roll them into a summary tree, build entity profiles, let unused claims decay and promote working memory. Every one is a rebuildable projection.',
      href: '/docs/dev/passes/jobs/',
    },
    read: {
      title: 'The read path',
      body: 'Every lane runs, always. One SQL statement unions them, one cross-encoder ranks the result by merit, and a budget walk keeps the longest prefix that fits.',
      href: '/docs/dev/read/overview/',
    },
    evidence: {
      title: 'One prompt-ready string',
      body: 'The answer is evidence, not a conclusion. Each item names its provenance class and the exact scopes it came from, so the agent can judge it.',
      href: '/docs/user/concepts/evidence/',
    },
  },
};

const write: Flow = {
  initial: 'gate',
  nodes: [
    box('text', 'authored text', 0, 0),
    box('uri', 'source URI', 210, 0),
    box('upload', 'agent upload', 420, 0),
    box('scan', 'ClamAV scan', 315, 100),
    box('convert', 'Docling convert', 315, 200),
    box('document', 'document', 160, 300),
    box('chunk', 'chunk and embed', 160, 400),
    box('gate', 'relevance gate', 160, 500),
    box('extract', 'extract', 160, 600),
    box('ground', 'ground on a quote', 160, 700),
    box('consolidate', 'consolidate', 160, 800),
  ],
  edges: [
    link('e1', 'text', 'document'),
    link('e2', 'uri', 'scan'),
    link('e3', 'upload', 'scan'),
    link('e4', 'scan', 'convert'),
    link('e5', 'convert', 'document'),
    link('a1', 'document', 'chunk'),
    link('a2', 'chunk', 'gate'),
    link('a3', 'gate', 'extract', 'relevant'),
    link('a4', 'extract', 'ground'),
    link('a5', 'ground', 'consolidate'),
  ],
  detail: {
    text: {
      title: 'Authored text',
      body: 'A note written by a person or an agent. It becomes a document directly with no conversion step, and it is the record that everything else is derived from.',
      href: '/docs/dev/write/intake/',
    },
    uri: {
      title: 'A source URI',
      body: 'aizk fetches the URL itself under bounded redirects and a timeout, so the caller never has to download and re-upload a page or a paper.',
      href: '/docs/dev/write/intake/',
    },
    upload: {
      title: 'An agent upload',
      body: 'MCP cannot carry file bytes in a tool call, so the agent declares size and hash, receives a single-use capability ticket, and PUTs the exact bytes out of band.',
      href: '/docs/dev/write/artifacts/',
    },
    scan: {
      title: 'Scanning comes first',
      body: 'Bytes are streamed through ClamAV before anything persists them, and the scan is fail-closed, so an unavailable scanner rejects the file rather than waving it through.',
      href: '/docs/dev/run/security/',
    },
    convert: {
      title: 'Conversion',
      body: 'Docling turns an accepted original into native JSON and normalized Markdown beside it. The original bytes are never replaced, and an unsupported type still records its metadata.',
      href: '/docs/dev/write/artifacts/',
    },
    document: {
      title: 'The document',
      body: 'The stored record, matched against an existing one by identity so a refresh updates rather than duplicates. This is the authority that recall ranks first.',
      href: '/docs/dev/store/content-tables/',
    },
    chunk: {
      title: 'Chunking and embedding',
      body: 'Normalized text is split, then each piece gets a vector from the pooled embedding lane. Chunks carry an ordinal so a document can be read back in order.',
      href: '/docs/dev/write/chunking/',
    },
    gate: {
      title: 'The relevance gate',
      body: 'A small GLiNER2 model decides whether a chunk has anything worth extracting. Most prose does not, and skipping those chunks is what makes the expensive lane affordable.',
      href: '/docs/dev/write/extraction/',
    },
    extract: {
      title: 'Extraction',
      body: 'One combined call returns entities and facts against a strict schema with hard field caps. Long text is windowed, and a context overflow bisects rather than failing.',
      href: '/docs/dev/write/extraction/',
    },
    ground: {
      title: 'Grounding',
      body: 'A fact survives only when the model quotes a contiguous span that really appears in the chunk. Ungrounded output is dropped rather than stored, which is the main quality gate.',
      href: '/docs/dev/write/consolidation/',
    },
    consolidate: {
      title: 'Consolidation',
      body: 'Rules resolve the confident cases, an exact match becomes a no-op, and only the ambiguous middle band goes to one batched model call. Contradictions close a range instead of deleting.',
      href: '/docs/dev/write/consolidation/',
    },
  },
};

const recall: Flow = {
  initial: 'merit',
  nodes: [
    box('query', 'question', 220, 0),
    box('embed', 'embed and seed entities', 220, 90),
    box('facts', 'facts', 0, 200),
    box('sources', 'sources', 190, 200),
    box('catalog', 'entity catalog', 380, 200),
    box('working', 'working memory', 0, 280),
    box('profile', 'profiles', 190, 280),
    box('communities', 'communities', 380, 280),
    box('overview', 'RAPTOR roots', 190, 360),
    box('union', 'one SQL statement', 190, 460),
    box('merit', 'cross-encoder merit order', 190, 560),
    box('pack', 'budget walk', 190, 660),
  ],
  edges: [
    link('a1', 'query', 'embed'),
    link('e1', 'embed', 'facts'),
    link('e2', 'embed', 'sources'),
    link('e3', 'embed', 'catalog'),
    link('e4', 'embed', 'working'),
    link('e5', 'embed', 'profile'),
    link('e6', 'embed', 'communities'),
    link('e7', 'embed', 'overview'),
    link('e8', 'facts', 'union'),
    link('e9', 'sources', 'union'),
    link('e10', 'catalog', 'union'),
    link('e11', 'working', 'union'),
    link('e12', 'profile', 'union'),
    link('e13', 'communities', 'union'),
    link('e14', 'overview', 'union'),
    link('a2', 'union', 'merit'),
    link('a3', 'merit', 'pack'),
  ],
  detail: {
    query: {
      title: 'The question',
      body: 'One natural-language question. The caller never selects a scope, because recall already spans the full visible union, and it never selects a lane either.',
      href: '/docs/user/using/recall/',
    },
    embed: {
      title: 'Embed and seed',
      body: 'The query vector and the named entities in the query are computed at the same time. Those two values are the only thing that shapes the SQL, which is why the statement caches so well.',
      href: '/docs/dev/read/overview/',
    },
    facts: {
      title: 'The fact lane',
      body: 'Dense seeds, then one hop to their neighbors, then a personalized PageRank diffusion when multi-hop is on. The parts are interleaved by rank rather than concatenated.',
      href: '/docs/dev/read/lanes/',
    },
    sources: {
      title: 'The source lane',
      body: 'Raw chunks through a hybrid of dense, lexical and title matching. Source text stays primary, because raw evidence outranks anything the engine summarized.',
      href: '/docs/dev/read/lanes/',
    },
    catalog: {
      title: 'The entity catalog lane',
      body: 'A compact roster of the current entities of a kind, which is what makes broad state questions like "what projects are open" answerable at all.',
      href: '/docs/dev/read/lanes/',
    },
    working: {
      title: 'Working memory',
      body: 'Recent session notes that have not yet been promoted into the graph. They carry their own provenance class so an agent can tell them from durable memory.',
      href: '/docs/dev/passes/promotion/',
    },
    profile: {
      title: 'Profiles',
      body: 'One evidence-grounded summary per entity and scope set, rebuilt when the entity goes dirty. A projection, so losing it costs compute rather than knowledge.',
      href: '/docs/dev/passes/profiles-insights/',
    },
    communities: {
      title: 'Communities',
      body: 'Louvain clusters with a label and a summary, which answer the broad corpus questions that no single chunk covers.',
      href: '/docs/dev/passes/communities-raptor/',
    },
    overview: {
      title: 'RAPTOR roots',
      body: 'The top level of a recursive summary tree, giving the broadest view of the corpus in a handful of rows.',
      href: '/docs/dev/passes/communities-raptor/',
    },
    union: {
      title: 'One statement',
      body: 'Every lane is unioned into a single materialized CTE and ordered by the plan. There is no query-time router deciding which lanes to run, because measurement did not support one.',
      href: '/docs/dev/read/lanes/',
    },
    merit: {
      title: 'Merit order',
      body: 'One cross-encoder scores the top candidates together, so a fact and a raw chunk compete on the same scale rather than each lane getting a reserved slot.',
      href: '/docs/dev/read/ranking/',
    },
    pack: {
      title: 'The budget walk',
      body: 'The longest prefix of the ranked list that fits the token budget is kept, and the rest is dropped. That is what stops recall from flooding a context window.',
      href: '/docs/dev/read/packing/',
    },
  },
};

export const flows: Record<string, Flow> = { system, write, recall };
