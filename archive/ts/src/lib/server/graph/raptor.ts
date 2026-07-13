// The recursive summary tree, the TS twin of graph/raptor.py: read one exact scope's
// communities and its stale previous generation as the system identity, plan the whole
// tree in memory (level-zero leaves from communities, greedy-modularity clustering of
// summary embeddings, one LLM rollup per multi-member cluster with near-duplicate parents
// reused), then atomically replace the generation in one owner-role transaction, since
// deleting shared entity content is structural work RLS would block.
import { sql } from 'drizzle-orm';
import { v7 as uuid7 } from 'uuid';

import { entityClaim, entityContent, factClaim, factContent } from '../db/schema';
import { embed, structured } from '../serving';
import { settings } from '../settings';
import { asSystem, bypassRls, cosine, scopeKey, toFloats, uuidArray, type Reporter } from './system';

const RAPTOR_SUMMARY = 'raptor_summary';
const PART_OF = 'part_of';

interface RaptorReport {
	label: string;
	summary: string;
}

const ROLLUP_SCHEMA = {
	name: 'RaptorReport',
	schema: {
		type: 'object',
		properties: {
			label: { type: 'string', description: 'short human-readable name for the broader theme' },
			summary: {
				type: 'string',
				description: 'one paragraph grounded only in the child summaries shown'
			}
		},
		required: ['label', 'summary'],
		additionalProperties: false
	}
};

const report: Reporter<RaptorReport> = (system, user) =>
	structured<RaptorReport>(system, user, ROLLUP_SCHEMA);

interface Node {
	entityId: string;
	label: string;
	summary: string;
	embedding: number[];
}

// Cluster summary embeddings by greedy (CNM) modularity over a similarity graph, the
// networkx greedy_modularity_communities semantics: start singleton, repeatedly merge the
// pair with the largest modularity gain while that gain stays positive, largest first.
export const cluster = (embeddings: number[][], threshold: number): number[][] => {
	const count = embeddings.length;
	const edges: [number, number][] = [];
	for (let left = 0; left < count; left++)
		for (let right = left + 1; right < count; right++)
			if (cosine(embeddings[left], embeddings[right]) >= threshold) edges.push([left, right]);
	if (!edges.length) return Array.from({ length: count }, (_, index) => [index]);
	const m = edges.length;
	const degree = new Array<number>(count).fill(0);
	const members = new Map<number, number[]>(
		Array.from({ length: count }, (_, index) => [index, [index]])
	);
	const between = new Map<number, Map<number, number>>();
	const link = (a: number, b: number, weight: number): void => {
		if (!between.has(a)) between.set(a, new Map());
		between.get(a)!.set(b, (between.get(a)!.get(b) ?? 0) + weight);
	};
	for (const [a, b] of edges) {
		degree[a] += 1;
		degree[b] += 1;
		link(a, b, 1);
		link(b, a, 1);
	}
	const totals = new Map<number, number>(degree.map((value, index) => [index, value]));
	for (;;) {
		let best: [number, number] | null = null;
		let bestGain = -Infinity;
		for (const [a, neighbors] of between)
			for (const [b, weight] of neighbors) {
				if (a >= b) continue;
				const gain = weight / m - (totals.get(a)! * totals.get(b)!) / (2 * m * (2 * m));
				if (gain > bestGain) {
					best = [a, b];
					bestGain = gain;
				}
			}
		// networkx merges while the best gain stays non-negative and stops at the first drop.
		if (!best || bestGain < 0) break;
		const [into, from] = best;
		members.get(into)!.push(...members.get(from)!);
		members.delete(from);
		totals.set(into, totals.get(into)! + totals.get(from)!);
		totals.delete(from);
		for (const [neighbor, weight] of between.get(from)!) {
			between.get(neighbor)!.delete(from);
			if (neighbor === into) continue;
			link(into, neighbor, weight);
			link(neighbor, into, weight);
		}
		between.delete(from);
		between.get(into)!.delete(from);
	}
	return [...members.values()]
		.map((group) => [...group].sort((a, b) => a - b))
		.sort((a, b) => b.length - a.length || a[0] - b[0]);
};

const redundantParent = (
	parents: [Node, number[]][],
	vector: number[],
	threshold: number
): Node | null =>
	parents.find(([, parentVector]) => cosine(parentVector, vector) >= threshold)?.[0] ?? null;

interface CommunityRow {
	id: string;
	label: string;
	summary: string;
	embedding: string;
}

// Plan a complete RAPTOR generation before its atomic database replacement, the
// RaptorBuilder twin holding staged contents, claims, edges, and edge claims.
class RaptorBuilder {
	contents: { id: string; name: string; type: string; embedding: string }[] = [];
	claims: {
		id: string;
		contentId: string;
		createdBy: string;
		scopes: string[];
		attributes: Record<string, unknown>;
	}[] = [];
	edges: {
		id: string;
		subjectId: string;
		objectId: string;
		predicate: string;
		statement: string;
	}[] = [];
	edgeClaims: { id: string; contentId: string; createdBy: string; scopes: string[] }[] = [];

	constructor(
		readonly key: string[],
		readonly rollup: Reporter<RaptorReport>
	) {}

	claim(contentId: string, level: number, summary: string): (typeof this.claims)[number] {
		return {
			id: uuid7(),
			contentId,
			createdBy: settings.systemUserId,
			scopes: this.key,
			attributes: { level, summary }
		};
	}

	leaves(communities: CommunityRow[]): Node[] {
		return communities.map((row) => {
			const contentId = uuid7();
			this.contents.push({
				id: contentId,
				name: row.label,
				type: RAPTOR_SUMMARY,
				embedding: row.embedding
			});
			const claim = this.claim(contentId, 0, row.summary);
			claim.attributes.community = row.id;
			this.claims.push(claim);
			return { entityId: contentId, label: row.label, summary: row.summary, embedding: toFloats(row.embedding) };
		});
	}

	connect(members: Node[], parent: Node): void {
		for (const member of members) {
			const edge = {
				id: uuid7(),
				subjectId: member.entityId,
				objectId: parent.entityId,
				predicate: PART_OF,
				statement: `is part of ${parent.label}`
			};
			this.edges.push(edge);
			this.edgeClaims.push({
				id: uuid7(),
				contentId: edge.id,
				createdBy: settings.systemUserId,
				scopes: this.key
			});
		}
	}

	async parent(
		members: Node[],
		level: number,
		parents: [Node, number[]][]
	): Promise<[Node, boolean]> {
		const rollup = await this.rollup(
			settings.raptorRollupSystem,
			'Child summaries:\n' + members.map((member) => `- ${member.label}: ${member.summary}`).join('\n')
		);
		const [literal] = await embed([rollup.summary], 'document');
		const vector = toFloats(literal);
		let parent = redundantParent(parents, vector, settings.raptorRedundancyThreshold);
		const created = parent === null;
		if (parent === null) {
			const contentId = uuid7();
			this.contents.push({
				id: contentId,
				name: rollup.label,
				type: RAPTOR_SUMMARY,
				embedding: literal
			});
			parent = { entityId: contentId, label: rollup.label, summary: rollup.summary, embedding: vector };
			this.claims.push(this.claim(contentId, level, rollup.summary));
			parents.push([parent, vector]);
		}
		this.connect(members, parent);
		return [parent, created];
	}

	async level(nodes: Node[], groups: number[][], level: number): Promise<[Node[], number]> {
		const nextNodes: Node[] = [];
		const nextIds = new Set<string>();
		const parents: [Node, number[]][] = [];
		let written = 0;
		for (const group of groups) {
			const members = group.map((index) => nodes[index]);
			const [parent, created] =
				members.length === 1 ? ([members[0], false] as [Node, boolean]) : await this.parent(members, level, parents);
			if (!nextIds.has(parent.entityId)) {
				nextNodes.push(parent);
				nextIds.add(parent.entityId);
			}
			written += created ? 1 : 0;
		}
		return [nextNodes, written];
	}

	async build(communities: CommunityRow[]): Promise<number> {
		if (communities.length < 2) return 0;
		let nodes = this.leaves(communities);
		let written = 0;
		let level = 1;
		while (nodes.length > settings.raptorRootMax && level <= settings.raptorMaxLevels) {
			const groups = cluster(nodes.map((node) => node.embedding), settings.raptorSimThreshold);
			if (groups.length >= nodes.length) break;
			const [nextNodes, count] = await this.level(nodes, groups, level);
			nodes = nextNodes;
			written += count;
			level += 1;
		}
		return written;
	}

	// Atomically delete the stale generation and insert the complete staged one.
	async replace(stale: string[]): Promise<void> {
		await bypassRls(async (tx) => {
			if (stale.length)
				await tx.execute(sql`delete from entity_content where id = any(${uuidArray(stale)}::uuid[])`);
			if (this.contents.length) await tx.insert(entityContent).values(this.contents);
			if (this.claims.length) await tx.insert(entityClaim).values(this.claims);
			if (this.edges.length) await tx.insert(factContent).values(this.edges);
			if (this.edgeClaims.length) await tx.insert(factClaim).values(this.edgeClaims);
		});
	}
}

// Build and atomically replace one exact scope's recursive summary tree, the build_raptor
// twin, returning the number of non-leaf summaries written.
export const buildRaptor = async (
	scopes?: string[],
	rollup: Reporter<RaptorReport> = report
): Promise<number> => {
	const key = scopeKey(scopes);
	const { communities, stale } = await asSystem(key, async (tx) => {
		const communities = (await tx.execute(sql`
			select id, label, summary, embedding from community
			where embedding is not null and scopes = ${uuidArray(key)}::uuid[]
		`)) as unknown as CommunityRow[];
		const stale = (await tx.execute(sql`
			select ec.content_id from entity_claim ec
			join entity_content e on e.id = ec.content_id
			where ec.scopes = ${uuidArray(key)}::uuid[] and e.type = ${RAPTOR_SUMMARY}
		`)) as unknown as { content_id: string }[];
		return { communities, stale: stale.map((row) => row.content_id) };
	});
	const builder = new RaptorBuilder(key, rollup);
	const written = await builder.build(communities);
	await builder.replace(stale);
	return written;
};
