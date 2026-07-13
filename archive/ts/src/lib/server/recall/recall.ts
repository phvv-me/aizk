// Recall orchestration, the port of the Python server's retrieval/recall.py: embed the
// query, classify its route and extract its entity mentions through the gliner sidecar,
// then run the recall program as the caller. One packed statement does everything by
// default; with a rerank endpoint configured the same statement stops before packing, the
// cross-encoder rescores the evidence lanes, and the packer twin replays the identical
// budget walk before one access-accounting update covers the kept facts.
import { sql } from 'drizzle-orm';
import { orderBy, pick } from 'es-toolkit';

import { settings } from '../settings';
import { actingAs, type User } from '../db';
import { classify, embed, mentions, rerank, rerankEnabled } from '../serving';
import { buildRecallStatement, type Route } from './query';

export interface Candidate {
	lane: string;
	line: string;
	fact_id: string | null;
	source_chunk_id: string | null;
	source_title: string | null;
	source_uri: string | null;
	created_by: string | null;
}

export interface CandidateRow extends Candidate {
	evidence_id: string;
	priority: number;
	lane_bit: number;
	ordering: number;
	line_tokens: number;
	header_tokens: number;
	used_tokens?: number;
}

export interface ContextPack {
	query: string;
	candidates: Candidate[];
	budget: number;
	used_tokens: number;
}

const RERANKABLE = new Set(['sources', 'facts', 'working_memory']);

const ROUTE_LABELS: Record<string, Route> = {
	'specific fact or entity lookup': 'local',
	'broad thematic overview or summary': 'global',
	'relationship or path between multiple entities': 'multihop'
};

const classifyRoute = async (query: string): Promise<Route> => {
	if (!settings.glinerGateUrl) return 'local';
	const label = await classify(query, 'memory retrieval route', Object.keys(ROUTE_LABELS));
	return ROUTE_LABELS[label] ?? 'local';
};

const queryMentions = async (query: string, entityTypes: string[]): Promise<string[]> =>
	settings.glinerGateUrl ? mentions(query, entityTypes) : [];

const shaped = (row: Candidate): Candidate => ({
	...pick(row, [
		'lane',
		'line',
		'fact_id',
		'source_chunk_id',
		'source_title',
		'source_uri',
		'created_by'
	]),
	fact_id: row.fact_id ?? null
});

// Greedily keep candidates while the token budget fits, the SQL packer's exact twin: each
// kept line costs its tokens, one separator, and its lane header the first time it opens.
export const pack = (candidates: CandidateRow[], budget: number): [CandidateRow[], number] => {
	const ranked = orderBy(
		candidates,
		[(row) => row.priority, (row) => row.ordering, (row) => row.evidence_id],
		['asc', 'asc', 'asc']
	);
	let used = 0;
	const opened = new Set<string>();
	const kept: CandidateRow[] = [];
	for (const candidate of ranked) {
		const header = opened.has(candidate.lane) ? 0 : candidate.header_tokens;
		const cost = candidate.line_tokens + header + 1;
		if (used + cost > budget) continue;
		used += cost;
		opened.add(candidate.lane);
		kept.push(candidate);
	}
	return [kept, used];
};

export const recall = async (
	query: string,
	user: User,
	k = 8,
	tokenBudget?: number,
	entityTypes: string[] = []
): Promise<ContextPack> => {
	const budget = tokenBudget ?? settings.contextTokenBudget;
	const searchQuery = user.label ? `${query}\nThe asking speaker is ${user.label}.` : query;
	const [[vector], route, named] = await Promise.all([
		embed([searchQuery], 'query'),
		classifyRoute(query),
		queryMentions(query, entityTypes)
	]);
	const reranking = rerankEnabled();
	const statement = buildRecallStatement(
		route,
		{ vector, text: searchQuery, mentions: named, k, budget },
		!reranking
	);
	const rows = (await actingAs(
		user,
		async (tx) => await tx.execute(statement)
	)) as unknown as CandidateRow[];
	if (!reranking) {
		return {
			query,
			candidates: rows.map(shaped),
			budget,
			used_tokens: rows.reduce((used, row) => Math.max(used, row.used_tokens ?? 0), 0)
		};
	}
	const evidence = rows.filter((row) => RERANKABLE.has(row.lane)).slice(0, settings.rerankDepth);
	const scores = await rerank(query, evidence.map((row) => row.line));
	const scored = new Map(evidence.map((row, index) => [row.evidence_id, scores[index]]));
	const rescored = rows.map((row) =>
		scored.has(row.evidence_id) ? { ...row, ordering: -(scored.get(row.evidence_id) ?? 0) } : row
	);
	const [kept, used] = pack(rescored, budget);
	const accessed = kept.flatMap((row) => (row.fact_id === null ? [] : [row.fact_id]));
	if (accessed.length)
		await actingAs(user, (tx) =>
			tx.execute(sql`
				update fact_claim set last_accessed = now(), access_count = access_count + 1
				where upper_inf(recorded) and id = any(${`{${accessed.join(',')}}`}::uuid[])
			`)
		);
	return { query, candidates: kept.map(shaped), budget, used_tokens: used };
};
