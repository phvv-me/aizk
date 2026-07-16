// The graph build ported from graph/build.py: list unprocessed chunks in one exact scope
// set, extract each chunk's journal and LLM slice, resolve entities, consolidate facts,
// and write the slice between short system-role transactions with transient-error retries
// and a four-round lock/revalidate/apply loop. PARITY SEAMS: extraction transport errors
// fail the build fast here where Python distinguishes timeout-skip from unreachable-abort,
// and the short-chunk length check counts UTF-16 units where Python counts code points.
import { sql, type SQL } from 'drizzle-orm';

import { withDocumentFallback } from '../extract/dating';
import * as journal from '../extract/journal';
import { decideConsolidationsBatch, extractGraph } from '../extract/llm';
import {
	captureFrom,
	observedAtDate,
	searchText,
	type CaptureContext,
	type ExtractedEntity,
	type TimedFact
} from '../extract/models';
import * as ontology from '../extract/ontology';
import { embedVectors, relevant } from '../serving';
import { settings } from '../settings';
import { cosineSimilarity } from './consolidation';
import { asSystem, scopeKey, uuidArray, type Tx } from './system';
import { GraphWriter, type FactPlan, type PreparedEntity } from './writer';

const nRaw = (value: number): SQL => sql.raw(value.toFixed(0));

export interface ChunkRow {
	id: string;
	document_id: string;
	text: string;
	provenance: Record<string, unknown>;
	created_by: string;
	scopes: string[];
}

interface DocumentRow {
	id: string;
	title: string | null;
	created_at: Date;
}

// Whether a database error is a transient deadlock or serialization failure worth retrying.
const isTransientDbError = (error: unknown): boolean => {
	const code = (error as { code?: string } | null)?.code;
	return code === '40001' || code === '40P01';
};

// Retry transient serialization failures up to four attempts with jittered backoff.
const retrying = async <T>(work: () => Promise<T>): Promise<T> => {
	for (let attempt = 0; ; attempt += 1) {
		try {
			return await work();
		} catch (error) {
			if (attempt >= 3 || !isTransientDbError(error)) throw error;
			const wait = Math.random() * Math.min(1000, 50 * 2 ** attempt);
			await new Promise((resolve) => setTimeout(resolve, wait));
		}
	}
};

// List unprocessed chunks in one exact scope set, in deterministic order.
export const pendingChunks = async (
	scopes: string[],
	limit: number | null,
	source: string | null
): Promise<ChunkRow[]> => {
	const key = scopeKey(scopes);
	const titled =
		source === null
			? sql``
			: sql` and document_id in (select id from document where title ilike ${`%${source}%`})`;
	const capped = limit === null ? sql`` : sql` limit ${nRaw(limit)}`;
	return asSystem(
		key,
		async (tx) =>
			(await tx.execute(sql`
				select id, document_id, text, provenance, created_by, scopes from chunk
				where processed_at is null and scopes = ${uuidArray(key)}::uuid[]${titled}
				order by id${capped}
			`)) as unknown as ChunkRow[]
	);
};

// Stamp one chunk's processed_at so pendingChunks never offers it again.
export const markProcessed = async (tx: Tx, chunkId: string): Promise<void> => {
	await tx.execute(sql`update chunk set processed_at = now() where id = ${chunkId}::uuid`);
};

// Return entity and fact claim counts in one exact scope set; the fact count spans the
// whole claim history including superseded versions.
export const graphCounts = async (scopes: string[]): Promise<[number, number]> => {
	const key = scopeKey(scopes);
	return asSystem(key, async (tx) => {
		const [row] = (await tx.execute(sql`
			select
				(select count(*) from entity_claim where scopes = ${uuidArray(key)}::uuid[]) as entities,
				(select count(*) from fact_claim where scopes = ${uuidArray(key)}::uuid[]) as facts
		`)) as unknown as { entities: string; facts: string }[];
		return [Number(row.entities), Number(row.facts)];
	});
};

// The structural type any sibling chunk of a document declares, Area or Project, else null.
const documentDeclaredType = async (tx: Tx, documentId: string): Promise<string | null> => {
	const siblings = (await tx.execute(
		sql`select text from chunk where document_id = ${documentId}::uuid`
	)) as unknown as { text: string }[];
	for (const sibling of siblings) {
		const declared = journal.declaredType(sibling.text);
		if (declared !== null) return declared;
	}
	return null;
};

// The note's declared title entity and any dated journal facts, empty when neither applies.
export const journalExtraction = async (
	tx: Tx,
	chunk: ChunkRow,
	document: DocumentRow | null
): Promise<[ExtractedEntity[], TimedFact[]]> => {
	if (document === null || !document.title) return [[], []];
	let declared = journal.declaredType(chunk.text);
	const hasJournal = journal.hasJournalEntries(chunk.text);
	if (declared === null && !hasJournal) return [[], []];
	if (declared === null) declared = await documentDeclaredType(tx, chunk.document_id);
	const entity = journal.titleEntity(document.title, declared);
	const facts = hasJournal ? journal.journalFacts(chunk.text, document.title) : [];
	return [[entity], facts];
};

// The combined-call entities and dated facts, empty when the chunk gates out.
export const llmExtraction = async (
	chunk: ChunkRow,
	document: DocumentRow | null
): Promise<[ExtractedEntity[], TimedFact[]]> => {
	if (settings.glinerGateEnabled && !(await relevant(chunk.text, ontology.gateLabels()))) {
		console.info(`chunk ${chunk.id} gated out, no ontology-relevant entities`);
		return [[], []];
	}
	const capture = captureFrom(chunk.provenance);
	const extraction = await extractGraph(searchText(capture, chunk.text));
	const fallback = observedAtDate(capture) ?? document?.created_at ?? new Date();
	return [extraction.entities, withDocumentFallback(extraction.facts, fallback)];
};

// Map one suggested-type embedding onto the declared ontology.
export const closestEntityType = (vector: number[]): string => {
	const scored = Object.entries(ontology.current().entityDescriptionVectors).map(
		([name, candidate]) => [name, cosineSimilarity(vector, candidate)] as const
	);
	if (!scored.length) return ontology.CONCEPT;
	const [name, similarity] = scored.reduce((best, next) => (next[1] > best[1] ? next : best));
	return similarity >= settings.ontologyMatchThreshold ? name : ontology.CONCEPT;
};

const uniq = <T>(values: T[]): T[] => [...new Set(values)];

// Resolve suggested types and embed entity names in one deduplicated model call.
export const prepareEntities = async (entities: ExtractedEntity[]): Promise<PreparedEntity[]> => {
	const suggestions = uniq(
		entities
			.filter((entity) => entity.type === ontology.CONCEPT && entity.suggestedType !== null)
			.map((entity) => entity.suggestedType as string)
	);
	const names = uniq(entities.map((entity) => entity.name));
	const texts = uniq([...suggestions, ...names]);
	const vectors = texts.length ? await embedVectors(texts, 'document') : [];
	const embedded = new Map(texts.map((text, index) => [text, vectors[index]]));
	const resolvedTypes = new Map(
		suggestions.map((suggestion) => [
			suggestion,
			closestEntityType(embedded.get(suggestion) as number[])
		])
	);
	return entities.map((entity) => ({
		name: entity.name,
		type: resolvedTypes.get(entity.suggestedType ?? '') ?? entity.type,
		vector: embedded.get(entity.name) as number[]
	}));
};

const matchesFingerprint = (plans: FactPlan[]): string =>
	JSON.stringify(plans.map((plan) => plan.matches));

// Plan model work between short entity, read, and final write transactions.
export const writeGraphSlice = async (
	chunk: ChunkRow,
	capture: CaptureContext,
	entities: ExtractedEntity[],
	datedFacts: TimedFact[]
): Promise<Set<string>> => {
	const key = scopeKey(chunk.scopes);
	const writer = new GraphWriter(chunk.created_by, chunk.scopes, capture, chunk.text);
	const prepared = await prepareEntities(entities);
	const { resolved, candidates } = await retrying(() =>
		asSystem(key, async (tx) => {
			const resolvedNames = await writer.resolveAll(tx, prepared);
			return {
				resolved: resolvedNames,
				candidates: await writer.newCandidates(tx, datedFacts, resolvedNames)
			};
		})
	);
	const vectors = candidates.length
		? await embedVectors(
				candidates.map((candidate) => candidate.fact.statement),
				'document'
			)
		: [];
	let plans = await asSystem(key, (tx) => writer.planFacts(tx, candidates, vectors));
	for (let round = 0; round < 4; round += 1) {
		const borderline = writer.borderline(plans);
		const decisions = borderline.length ? await decideConsolidationsBatch(borderline) : [];
		const staged = plans;
		const outcome = await retrying(() =>
			asSystem(key, async (tx) => {
				await writer.lockPlans(tx, staged);
				const current = await writer.planFacts(tx, candidates, vectors);
				if (matchesFingerprint(current) !== matchesFingerprint(staged))
					return { applied: false, current };
				await writer.applyPlans(tx, staged, decisions, chunk.id);
				await markProcessed(tx, chunk.id);
				return { applied: true, current };
			})
		);
		if (outcome.applied) return new Set(resolved.values());
		plans = outcome.current;
	}
	throw new Error(`graph slice ${chunk.id} changed during four consolidation attempts`);
};

// Extract, resolve, and consolidate one chunk's graph slice, return the entities it touched.
export const extractAndConsolidate = async (chunk: ChunkRow): Promise<Set<string>> => {
	const key = scopeKey(chunk.scopes);
	const { document, journalEntities, journalFacts } = await asSystem(key, async (tx) => {
		const [row] = (await tx.execute(
			sql`select id, title, created_at from document where id = ${chunk.document_id}::uuid`
		)) as unknown as (Omit<DocumentRow, 'created_at'> & { created_at: string | Date })[];
		// postgres-js may hand timestamptz back unparsed through drizzle's execute.
		const found = row === undefined ? null : { ...row, created_at: new Date(row.created_at) };
		const [entities, facts] = await journalExtraction(tx, chunk, found);
		return { document: found, journalEntities: entities, journalFacts: facts };
	});
	let entities = journalEntities;
	let datedFacts = journalFacts;
	const short = chunk.text.trim().length < settings.extractMinChars;
	if (short && !datedFacts.length) {
		await asSystem(key, (tx) => markProcessed(tx, chunk.id));
		return new Set();
	}
	if (!short) {
		const [llmEntities, llmFacts] = await llmExtraction(chunk, document);
		entities = [...entities, ...llmEntities];
		datedFacts = [...datedFacts, ...llmFacts];
	}
	const touched = await writeGraphSlice(chunk, captureFrom(chunk.provenance), entities, datedFacts);
	console.info(`graph slice from chunk ${chunk.id} done`);
	return touched;
};

// Run tasks through a bounded worker pool, the extraction semaphore twin.
const pooled = async <T>(tasks: (() => Promise<T>)[], width: number): Promise<T[]> => {
	const results = new Array<T>(tasks.length);
	let next = 0;
	const worker = async (): Promise<void> => {
		for (;;) {
			const index = next++;
			if (index >= tasks.length) return;
			results[index] = await tasks[index]();
		}
	};
	await Promise.all(Array.from({ length: Math.max(1, width) }, worker));
	return results;
};

// Build the graph from chunks the build has never run over and return the counts created.
export const buildGraph = async (
	options: { limit?: number | null; scopes?: string[]; source?: string | null } = {}
): Promise<[number, number]> => {
	const key = scopeKey(options.scopes);
	await asSystem(key, (tx) => ontology.ensureCurrent(tx));
	const chunks = await pendingChunks(key, options.limit ?? null, options.source ?? null);
	const [entitiesBefore, factsBefore] = await graphCounts(key);
	await pooled(
		chunks.map((chunk) => () => extractAndConsolidate(chunk)),
		settings.graphBuildConcurrency
	);
	const [entitiesAfter, factsAfter] = await graphCounts(key);
	return [entitiesAfter - entitiesBefore, factsAfter - factsBefore];
};
