// Entity communities over the latest-fact graph, the TS twin of graph/communities.py:
// snapshot the visible live facts and entity roster as the system identity, detect
// clusters by Louvain modularity, summarize and embed each, then replace the scope's whole
// community generation in one app-role transaction. Louvain is inlined below with the
// standard local-move plus aggregation phases; networkx's seeded shuffle is mirrored by a
// small deterministic PRNG, so partitions are stable but not bit-identical to Python's.
import { sql } from 'drizzle-orm';
import { v7 as uuid7 } from 'uuid';

import { community } from '../db/schema';
import { embed, structured } from '../serving';
import { settings } from '../settings';
import { asSystem, scopeKey, uuidArray, type Reporter } from './system';

interface LiveFactRow {
	subject_id: string;
	object_id: string | null;
	statement: string;
}

interface CommunitySummary {
	label: string;
	summary: string;
}

const SUMMARY_SCHEMA = {
	name: 'CommunitySummary',
	schema: {
		type: 'object',
		properties: {
			label: { type: 'string', description: 'short human-readable name for the cluster theme' },
			summary: { type: 'string', description: 'one paragraph grounded only in the facts shown' }
		},
		required: ['label', 'summary'],
		additionalProperties: false
	}
};

const report: Reporter<CommunitySummary> = (system, user) =>
	structured<CommunitySummary>(system, user, SUMMARY_SCHEMA);

const mulberry32 = (seed: number): (() => number) => {
	let state = seed >>> 0;
	return () => {
		state = (state + 0x6d2b79f5) | 0;
		let t = Math.imul(state ^ (state >>> 15), 1 | state);
		t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
		return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
	};
};

const shuffled = (count: number, random: () => number): number[] => {
	const order = Array.from({ length: count }, (_, index) => index);
	for (let i = count - 1; i > 0; i--) {
		const j = Math.floor(random() * (i + 1));
		[order[i], order[j]] = [order[j], order[i]];
	}
	return order;
};

// Weighted undirected adjacency per node plus per-node self-loop weight (aggregation
// folds a community's internal edges into a self loop, exactly the Louvain construction).
interface Level {
	adjacency: Map<number, number>[];
	loops: number[];
}

const degrees = (level: Level): number[] =>
	level.adjacency.map((neighbors, node) => {
		let degree = 2 * level.loops[node];
		for (const weight of neighbors.values()) degree += weight;
		return degree;
	});

// One local-moving phase: each node greedily joins the neighbor community with the best
// modularity gain until a full pass moves nothing. Returns the community per node.
const movePhase = (
	level: Level,
	random: () => number
): { membership: number[]; moved: boolean } => {
	const nodeCount = level.adjacency.length;
	const degree = degrees(level);
	const twoM = degree.reduce((sum, value) => sum + value, 0);
	const membership = Array.from({ length: nodeCount }, (_, index) => index);
	const communityDegree = [...degree];
	let moved = false;
	if (twoM === 0) return { membership, moved };
	for (let improved = true; improved; ) {
		improved = false;
		for (const node of shuffled(nodeCount, random)) {
			const current = membership[node];
			communityDegree[current] -= degree[node];
			const weights = new Map<number, number>([[current, 0]]);
			for (const [neighbor, weight] of level.adjacency[node])
				weights.set(
					membership[neighbor],
					(weights.get(membership[neighbor]) ?? 0) + weight
				);
			let best = current;
			let bestGain = (weights.get(current) ?? 0) - (communityDegree[current] * degree[node]) / twoM;
			for (const [candidate, weight] of weights) {
				const gain = weight - (communityDegree[candidate] * degree[node]) / twoM;
				if (gain > bestGain) {
					best = candidate;
					bestGain = gain;
				}
			}
			membership[node] = best;
			communityDegree[best] += degree[node];
			if (best !== current) {
				improved = true;
				moved = true;
			}
		}
	}
	return { membership, moved };
};

const aggregate = (level: Level, membership: number[], compact: Map<number, number>): Level => {
	const adjacency: Map<number, number>[] = Array.from({ length: compact.size }, () => new Map());
	const loops = new Array<number>(compact.size).fill(0);
	membership.forEach((communityId, node) => {
		const home = compact.get(communityId)!;
		loops[home] += level.loops[node];
		for (const [neighbor, weight] of level.adjacency[node]) {
			const away = compact.get(membership[neighbor])!;
			if (away === home) loops[home] += weight / 2;
			else adjacency[home].set(away, (adjacency[home].get(away) ?? 0) + weight);
		}
	});
	return { adjacency, loops };
};

const louvain = (nodeCount: number, edges: [number, number][], seed: number): number[][] => {
	const random = mulberry32(seed);
	let level: Level = {
		adjacency: Array.from({ length: nodeCount }, () => new Map()),
		loops: new Array<number>(nodeCount).fill(0)
	};
	for (const [a, b] of edges) {
		level.adjacency[a].set(b, 1);
		level.adjacency[b].set(a, 1);
	}
	let assignment = Array.from({ length: nodeCount }, (_, index) => index);
	for (;;) {
		const { membership, moved } = movePhase(level, random);
		if (!moved) break;
		const compact = new Map<number, number>();
		for (const communityId of membership)
			if (!compact.has(communityId)) compact.set(communityId, compact.size);
		assignment = assignment.map((communityId) => compact.get(membership[communityId])!);
		if (compact.size === level.adjacency.length) break;
		level = aggregate(level, membership, compact);
	}
	const groups = new Map<number, number[]>();
	assignment.forEach((communityId, node) => {
		const group = groups.get(communityId) ?? [];
		group.push(node);
		groups.set(communityId, group);
	});
	return [...groups.values()];
};

// Detect entity communities over the latest-fact graph by Louvain modularity, the twin of
// detect(): nodes are the entities related facts connect, edges deduplicate fact pairs.
export const detect = (facts: LiveFactRow[], minSize: number): string[][] => {
	const index = new Map<string, number>();
	const nodes: string[] = [];
	const edges: [number, number][] = [];
	const seen = new Set<string>();
	for (const fact of facts) {
		if (fact.object_id === null || fact.object_id === fact.subject_id) continue;
		const at = (id: string): number => {
			if (!index.has(id)) {
				index.set(id, nodes.length);
				nodes.push(id);
			}
			return index.get(id)!;
		};
		const [a, b] = [at(fact.subject_id), at(fact.object_id)].sort((x, y) => x - y);
		if (seen.has(`${a}|${b}`)) continue;
		seen.add(`${a}|${b}`);
		edges.push([a, b]);
	}
	if (!edges.length) return [];
	return louvain(nodes.length, edges, settings.louvainSeed)
		.filter((members) => members.length >= minSize)
		.map((members) => members.map((node) => nodes[node]).sort());
};

// Render one cluster's entity roster and internal facts, the CommunityBuilder.prompt twin.
const prompt = (cluster: string[], entities: Map<string, string>, facts: LiveFactRow[]): string => {
	const members = new Set(cluster);
	const names = cluster.filter((member) => entities.has(member)).map((member) => entities.get(member)!);
	const statements = facts
		.filter(
			(fact) =>
				members.has(fact.subject_id) && (fact.object_id === null || members.has(fact.object_id))
		)
		.map((fact) => fact.statement);
	const roster = 'Entities: ' + names.join(', ');
	return `${roster}\n\nFacts:\n${statements.map((statement) => `- ${statement}`).join('\n')}`;
};

// Detect communities over the entity graph, summarize each, store the rows, return the
// count, the build_communities twin.
export const buildCommunities = async (
	scopes?: string[],
	summarize: Reporter<CommunitySummary> = report
): Promise<number> => {
	const key = scopeKey(scopes);
	const { facts, entities } = await asSystem(key, async (tx) => {
		const facts = (await tx.execute(sql`
			select subject_id, object_id, statement from live_fact where embedding is not null
		`)) as unknown as LiveFactRow[];
		const entityIds = [
			...new Set(facts.flatMap((fact) => [fact.subject_id, fact.object_id ?? fact.subject_id]))
		];
		const roster = entityIds.length
			? ((await tx.execute(sql`
				select id, name from entity_content where id = any(${uuidArray(entityIds)}::uuid[])
			`)) as unknown as { id: string; name: string }[])
			: [];
		return { facts, entities: new Map(roster.map((row) => [row.id, row.name])) };
	});
	const clusters = detect(facts, settings.communityMinSize);
	const reports: CommunitySummary[] = [];
	for (const cluster of clusters)
		reports.push(await summarize(settings.communitySummarySystem, prompt(cluster, entities, facts)));
	const vectors = await embed(reports.map((item) => item.summary), 'document');
	const rows = clusters.map((cluster, index) => ({
		id: uuid7(),
		createdBy: settings.systemUserId,
		scopes: key,
		label: reports[index].label,
		summary: reports[index].summary,
		embedding: vectors[index],
		memberIds: cluster
	}));
	await asSystem(key, async (tx) => {
		await tx.execute(sql`delete from community where scopes = ${uuidArray(key)}::uuid[]`);
		if (rows.length) await tx.insert(community).values(rows);
	});
	return rows.length;
};
