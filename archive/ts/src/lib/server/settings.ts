// Runtime configuration read from AIZK_-prefixed environment variables, the same names and
// defaults as the Python server's config/settings.py so both servers tune identically.
import { env } from 'node:process';

const number = (name: string, fallback: number): number => {
	const raw = env[`AIZK_${name}`];
	return raw === undefined || raw === '' ? fallback : Number(raw);
};

const text = (name: string, fallback: string): string => env[`AIZK_${name}`] ?? fallback;

const flag = (name: string, fallback: boolean): boolean => {
	const raw = env[`AIZK_${name}`];
	return raw === undefined || raw === '' ? fallback : !['0', 'false', ''].includes(raw);
};

// Prompt defaults copied verbatim from the Python config/settings.py module constants so the
// extraction pipeline sends byte-identical system turns from either server.
const consolidationPrompt =
	'You maintain a bi-temporal knowledge graph. A non-LLM cascade already resolved every new\n' +
	'fact whose similarity to an existing fact was unambiguous; you only see the genuinely\n' +
	'borderline ones, numbered, each with its own catalog of similar existing facts. For each\n' +
	'numbered item decide one action.\n' +
	'ADD when the new fact states something none of its own existing facts cover.\n' +
	'UPDATE when the new fact supersedes one of its own existing facts, such as a changed value\n' +
	"or status, and name that fact's id in supersedes.\n" +
	'NOOP when one of its own existing facts already states the same thing.\n' +
	'Return exactly one verdict per numbered item shown, in the same order.';
const extractPreferencesPrompt =
	'Extract the durable preferences, decisions, and habits the text reveals about a person or a\n' +
	'project, never the transient facts. Prefer Decision, Pattern, and Gotcha entities and the\n' +
	'because, avoids, and uses relations that record why a choice holds, so the graph captures\n' +
	'how the subject prefers to work rather than what a document happens to state.';
const extractSummaryPrompt =
	'Extract only the few highest-level entities the span is about and the claims that summarize\n' +
	'it, never the incidental details. Prefer Concept, Claim, and Result entities and the\n' +
	"relations that connect the span's main subject to what it asserts, so the graph reads as a\n" +
	'summary of the span rather than an exhaustive transcription.';
const extractSystemPrompt =
	'Extract only the entities and facts the document text actually asserts. Never describe this\n' +
	'prompt, the ontology, the extraction task, or the json format as if they were content.\n' +
	'Write every entity name and fact statement in English, whatever language the source text\n' +
	'uses, and name the author of a first-person statement by role, the author, never a bare\n' +
	'pronoun such as "I".\n' +
	'Write every entity name as a plain human-readable noun phrase, never a slug, file name,\n' +
	'kebab-case token, or code identifier, so team-memory-spine becomes team memory spine.\n' +
	'Choose the single entity type that most precisely fits the thing, and when nothing fits\n' +
	'use Concept rather than forcing an unrelated type. Each fact must read true as subject\n' +
	'predicate object, and its statement must stand on its own without the surrounding text.\n' +
	'Give each fact a quote, the shortest excerpt copied verbatim from the text that supports\n' +
	'it, so the fact stays anchored to its exact source span.\n\n' +
	'Classify each fact as world for objective shared state, experience for something a speaker\n' +
	'did or encountered, observation for something a speaker perceived, opinion for a belief or\n' +
	'judgment, preference for a durable choice, procedure for reusable steps, or negative_result\n' +
	'for an attempted approach that failed. Keep the named speaker in every non-world\n' +
	'statement.\n\n' +
	'Example.\n' +
	'Text: "The team-memory-spine project uses Graphiti for bi-temporal storage, building on the\n' +
	'work of the Zep authors."\n' +
	'Entities: team memory spine (Project), Graphiti (Tool), Zep (Paper).\n' +
	'Facts: team memory spine uses Graphiti; Graphiti extends Zep.';
const ontologyPromptTemplate =
	'\nExtract a knowledge graph using only the controlled vocabularies below.\n\n' +
	'Entity types ({entity_count}):\n{entity_types}\n\n' +
	'Relation types ({relation_count}):\n{relation_types}\n\n' +
	'Rules.\n' +
	'Use only the entity types and relation types listed above, never invent new ones.\n' +
	'Every fact is a subject entity, a relation type as the predicate, and an object entity.\n' +
	'Write each entity name in its canonical singular form, lowercase unless a proper noun.\n' +
	'Write a one-sentence statement for each fact that stands on its own.\n' +
	'Drop any candidate fact whose predicate is not in the relation list.\n';

export const settings = {
	databaseUrl: text(
		'DATABASE_URL',
		`postgresql://aizk_app:${text('APP_PASSWORD', 'aizk_app')}@${text('DB_HOST', 'localhost')}:${number('DB_PORT', 5433)}/${text('DB_NAME', 'aizk')}`
	),
	adminDatabaseUrl: text(
		'ADMIN_DATABASE_URL',
		`postgresql://aizk_admin:${text('ADMIN_PASSWORD', 'aizk')}@${text('DB_HOST', 'localhost')}:${number('DB_PORT', 5433)}/${text('DB_NAME', 'aizk')}`
	),
	bm25Backend: text('BM25_BACKEND', 'vchord_bm25'),
	// AIZK_CHUNK_URL and AIZK_CHUNKER are TS-only seams: Python chunks in process with
	// chonkie, while this server posts to a /chunk sidecar (defaulting to the gliner gate
	// host) or, for verification only, splits naively on blank lines with AIZK_CHUNKER=naive.
	chunkSize: number('CHUNK_SIZE', 2048),
	chunkUrl: text('CHUNK_URL', text('GLINER_GATE_URL', '')),
	chunker: text('CHUNKER', ''),
	communitiesCron: text('COMMUNITIES_CRON', '0 4 * * 0'),
	communitiesEnabled: flag('COMMUNITIES_ENABLED', true),
	communitiesEveryNFacts: number('COMMUNITIES_EVERY_N_FACTS', 50),
	communityMinSize: number('COMMUNITY_MIN_SIZE', 3),
	communityRecallK: number('COMMUNITY_RECALL_K', 3),
	communitySummarySystem: text(
		'COMMUNITY_SUMMARY_SYSTEM',
		"You summarize one cluster of a knowledge graph. Given the cluster's entities and the facts\n" +
			"among them, write a short label naming the theme and a one-paragraph summary of what the\n" +
			'cluster is about. Ground every word in the facts shown, never invent detail, and write the\n' +
			'summary so a reader asking a broad question about this area would recognize it as relevant.'
	),
	consolidationAutoMergeThreshold: number('CONSOLIDATION_AUTO_MERGE_THRESHOLD', 0.9),
	consolidationBorderlineFloor: number('CONSOLIDATION_BORDERLINE_FLOOR', 0.75),
	consolidationPrompt: text('CONSOLIDATION_PROMPT', consolidationPrompt),
	contextTokenBudget: number('CONTEXT_TOKEN_BUDGET', 2048),
	contextualBm25: flag('CONTEXTUAL_BM25', false),
	decayCron: text('DECAY_CRON', '0 3 * * *'),
	decayEnabled: flag('DECAY_ENABLED', true),
	decayFloor: number('DECAY_FLOOR', 0.25),
	decayHalfLifeDays: number('DECAY_HALF_LIFE_DAYS', 90.0),
	dedupCron: text('DEDUP_CRON', '30 3 * * *'),
	dedupEnabled: flag('DEDUP_ENABLED', true),
	embedBatchSize: number('EMBED_BATCH_SIZE', 32),
	embedDim: number('EMBED_DIM', 1024),
	embedInstructionQuery: text(
		'EMBED_INSTRUCTION_QUERY',
		'Given a search query, retrieve relevant passages that answer it.'
	),
	embedModel: text('EMBED_MODEL', 'qwen3-vl-emb'),
	embedUrl: text('EMBED_URL', 'http://localhost:8000/v1'),
	entityResolutionThreshold: number('ENTITY_RESOLUTION_THRESHOLD', 0.85),
	extractCustomPrompt: text('EXTRACT_CUSTOM_PROMPT', ''),
	extractMaxTokens: number('EXTRACT_MAX_TOKENS', 2048),
	extractMinChars: number('EXTRACT_MIN_CHARS', 80),
	extractPreferencesPrompt: text('EXTRACT_PREFERENCES_PROMPT', extractPreferencesPrompt),
	extractStrategy: text('EXTRACT_STRATEGY', 'ontology'),
	extractSummaryPrompt: text('EXTRACT_SUMMARY_PROMPT', extractSummaryPrompt),
	extractSystemPrompt: text('EXTRACT_SYSTEM_PROMPT', extractSystemPrompt),
	extractTemperature: number('EXTRACT_TEMPERATURE', 0.0),
	extractTimeout: number('EXTRACT_TIMEOUT', 90.0),
	factCandidateFactor: number('FACT_CANDIDATE_FACTOR', 2),
	fusionDepth: number('FUSION_DEPTH', 50),
	glinerGateEnabled: flag('GLINER_GATE_ENABLED', true),
	glinerGateFloor: text('GLINER_GATE_FLOOR', 'Person'),
	glinerGateThreshold: number('GLINER_GATE_THRESHOLD', 0.7),
	glinerGateTimeout: number('GLINER_GATE_TIMEOUT', 30.0),
	glinerGateUrl: text('GLINER_GATE_URL', ''),
	graphBuildConcurrency: number('GRAPH_BUILD_CONCURRENCY', 48),
	graphDanglingFactor: number('GRAPH_DANGLING_FACTOR', 0.5),
	graphEntitySeedWeight: number('GRAPH_ENTITY_SEED_WEIGHT', 1.0),
	graphFactSeedWeight: number('GRAPH_FACT_SEED_WEIGHT', 0.25),
	graphFactsK: number('GRAPH_FACTS_K', 20),
	graphMassWindow: number('GRAPH_MASS_WINDOW', 80),
	graphMentionFuzzy: flag('GRAPH_MENTION_FUZZY', true),
	graphMentionMass: number('GRAPH_MENTION_MASS', 10.0),
	graphPprDamping: number('GRAPH_PPR_DAMPING', 0.5),
	graphPprFrontier: number('GRAPH_PPR_FRONTIER', 32),
	graphSeedEntities: number('GRAPH_SEED_ENTITIES', 16),
	insightCron: text('INSIGHT_CRON', '0 7 * * 0'),
	insightEnabled: flag('INSIGHT_ENABLED', true),
	insightFactsK: number('INSIGHT_FACTS_K', 40),
	insightMax: number('INSIGHT_MAX', 5),
	insightMinSignificance: number('INSIGHT_MIN_SIGNIFICANCE', 0.6),
	insightSystem: text(
		'INSIGHT_SYSTEM',
		'You study the facts already recorded about one graph and derive higher-level observations\n' +
			'they jointly support. Write only observations grounded in the facts shown, never restating\n' +
			'a single fact and never inventing detail beyond them, and score each by how much it adds\n' +
			'over the facts it rests on. Prefer a few significant patterns to many shallow restatements.'
	),
	llmApiKey: text('LLM_API_KEY', ''),
	llmModel: text('LLM_MODEL', 'gemma4-e2b-llm'),
	llmProvider: text('LLM_PROVIDER', 'vllm'),
	llmUrl: text('LLM_URL', 'http://localhost:8002/v1'),
	louvainSeed: number('LOUVAIN_SEED', 7),
	multihopMaxHops: number('MULTIHOP_MAX_HOPS', 2),
	ontologyMatchThreshold: number('ONTOLOGY_MATCH_THRESHOLD', 0.85),
	ontologyPromptTemplate: text('ONTOLOGY_PROMPT_TEMPLATE', ontologyPromptTemplate),
	profileOnWrite: flag('PROFILE_ON_WRITE', true),
	profileRecallK: number('PROFILE_RECALL_K', 1),
	profileRefreshCron: text('PROFILE_REFRESH_CRON', '0 5 * * 0'),
	profileRefreshEnabled: flag('PROFILE_REFRESH_ENABLED', true),
	profileSystem: text(
		'PROFILE_SYSTEM',
		'You write a short profile of one entity from the facts about it. Open with the stable,\n' +
			'static identity of the thing, what it is and what it is for, then add the dynamic state the\n' +
			'latest facts assert, its current status, values, and relations. Ground every word in the\n' +
			'facts shown, never invent detail, and write one tight paragraph a reader could lift whole.'
	),
	profiles: flag('PROFILES', true),
	promotedBonus: number('PROMOTED_BONUS', 0.01),
	raptor: flag('RAPTOR', true),
	raptorCron: text('RAPTOR_CRON', '30 4 * * 0'),
	raptorEnabled: flag('RAPTOR_ENABLED', true),
	raptorEveryNFacts: number('RAPTOR_EVERY_N_FACTS', 50),
	raptorK: number('RAPTOR_K', 3),
	raptorMaxLevels: number('RAPTOR_MAX_LEVELS', 5),
	raptorRedundancyThreshold: number('RAPTOR_REDUNDANCY_THRESHOLD', 0.95),
	raptorRollupSystem: text(
		'RAPTOR_ROLLUP_SYSTEM',
		'You merge several cluster summaries that sit one level below into a single higher-level\n' +
			'summary. Given the child summaries, write a short label naming the broader theme they share\n' +
			'and a one-paragraph summary of what that theme covers. Ground every word in the child\n' +
			'summaries shown, never invent detail, and write so a reader asking a broad question about\n' +
			'this whole area would recognize it as relevant.'
	),
	raptorRootMax: number('RAPTOR_ROOT_MAX', 3),
	raptorSimThreshold: number('RAPTOR_SIM_THRESHOLD', 0.5),
	recallCharsPerToken: number('RECALL_CHARS_PER_TOKEN', 4.0),
	recallFrequencyWeight: number('RECALL_FREQUENCY_WEIGHT', 0.02),
	recallMaxDistance: number('RECALL_MAX_DISTANCE', 0.65),
	recallPerDocument: number('RECALL_PER_DOCUMENT', 3),
	recallRecencyHalfLifeDays: number('RECALL_RECENCY_HALF_LIFE_DAYS', 30.0),
	recallRecencyWeight: number('RECALL_RECENCY_WEIGHT', 0.1),
	reembedBatch: number('REEMBED_BATCH', 128),
	rerankApiKey: text('RERANK_API_KEY', ''),
	rerankDepth: number('RERANK_DEPTH', 50),
	rerankDocumentTemplate: text(
		'RERANK_DOCUMENT_TEMPLATE',
		'<Document>: {document}<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'
	),
	rerankInstruction: text(
		'RERANK_INSTRUCTION',
		'Given a question about stored memory, judge whether the evidence answers it.'
	),
	rerankModel: text('RERANK_MODEL', 'qwen3-reranker'),
	rerankQueryTemplate: text(
		'RERANK_QUERY_TEMPLATE',
		'<|im_start|>system\nJudge whether the Document meets the requirements based on the' +
			' Query and the Instruct provided. Note that the answer can only be "yes" or' +
			' "no".<|im_end|>\n<|im_start|>user\n<Instruct>: {instruction}\n<Query>: {query}\n'
	),
	rerankRequestTimeout: number('RERANK_REQUEST_TIMEOUT', 30.0),
	rerankUrl: text('RERANK_URL', ''),
	rrfK: number('RRF_K', 60),
	sessionPromoteAgeMinutes: number('SESSION_PROMOTE_AGE_MINUTES', 60.0),
	sessionPromoteCron: text('SESSION_PROMOTE_CRON', '*/15 * * * *'),
	sessionPromoteEnabled: flag('SESSION_PROMOTE_ENABLED', true),
	sessionPromoteThreshold: number('SESSION_PROMOTE_THRESHOLD', 20),
	sessionRecallK: number('SESSION_RECALL_K', 5),
	similarFacts: number('SIMILAR_FACTS', 5),
	snippetChars: number('SNIPPET_CHARS', 280),
	systemUserId: text('SYSTEM_USER_ID', '00000000-0000-0000-0000-000000000001')
};
