// The reflective pass, the TS twin of graph/insight.py: read the latest fact statements as
// the system identity, ask the model for higher-level observations, gate them by
// significance, embed the keepers, and write each as a content-addressed observes fact
// hanging off one per-scope observation node, idempotent across runs.
import { sql } from 'drizzle-orm';

import { embed, structured } from '../serving';
import { settings } from '../settings';
import { entityId, factId } from './ids';
import { asSystem, scopeKey, uuidArray, type Reporter, type Tx } from './system';

const OBSERVATION = 'observation';
const OBSERVES = 'observes';

// The single node every observation hangs off, one per user, so the derived insights form
// one small structural subgraph the recall fact lane already surfaces.
const OBSERVATION_NODE = 'graph observations';

interface Observation {
	statement: string;
	significance: number;
}

interface InsightReport {
	observations: Observation[];
}

const INSIGHT_SCHEMA = {
	name: 'InsightReport',
	schema: {
		type: 'object',
		properties: {
			observations: {
				type: 'array',
				items: {
					type: 'object',
					properties: {
						statement: {
							type: 'string',
							description: 'a self-contained insight grounded only in the facts shown'
						},
						significance: {
							type: 'number',
							minimum: 0,
							maximum: 1,
							description: 'how much the insight adds beyond the facts, from 0 to 1'
						}
					},
					required: ['statement', 'significance'],
					additionalProperties: false
				}
			}
		},
		required: ['observations'],
		additionalProperties: false
	}
};

const report: Reporter<InsightReport> = (system, user) =>
	structured<InsightReport>(system, user, INSIGHT_SCHEMA);

// The observations that clear the significance gate, capped at the per-run write limit.
const keptObservations = (insight: InsightReport): Observation[] =>
	insight.observations
		.filter((obs) => obs.significance >= settings.insightMinSignificance)
		.sort((a, b) => b.significance - a.significance)
		.slice(0, settings.insightMax);

// Whether this scope already stakes an observes claim on this content id, ever, so the
// check reads past the live gate the recall lanes apply.
const alreadyClaimed = async (tx: Tx, key: string[], identity: string): Promise<boolean> => {
	const claimed = (await tx.execute(sql`
		select id from fact_claim
		where content_id = ${identity} and scopes = ${uuidArray(key)}::uuid[]
		limit 1
	`)) as unknown as { id: string }[];
	return claimed.length > 0;
};

// Idempotently write one gated observation as an observes fact, returning whether it was
// new. Content minting tolerates the deterministic-id race exactly like the Python mint().
const writeObservation = async (
	tx: Tx,
	key: string[],
	nodeId: string,
	obs: Observation,
	vector: string
): Promise<boolean> => {
	const identity = factId(OBSERVATION_NODE, OBSERVES, '', obs.statement);
	if (await alreadyClaimed(tx, key, identity)) return false;
	await tx.execute(sql`
		insert into fact_content (id, subject_id, object_id, predicate, statement, embedding)
		values (${identity}, ${nodeId}, null, ${OBSERVES}, ${obs.statement}, ${vector}::halfvec)
		on conflict do nothing
	`);
	await tx.execute(sql`
		insert into fact_claim (id, content_id, created_by, scopes, attributes)
		values (uuidv7(), ${identity}, ${settings.systemUserId}, ${uuidArray(key)}::uuid[],
			jsonb_build_object('significance', ${obs.significance}::float8))
		on conflict (content_id, scopes, perspective_key) where upper_inf(recorded) do nothing
	`);
	return true;
};

// Derive observations from a user's graph and write the significant ones back, the
// derive_insights twin, returning how many were written.
export const deriveInsights = async (
	scopes?: string[],
	reflect: Reporter<InsightReport> = report
): Promise<number> => {
	const key = scopeKey(scopes);
	const grounding = await asSystem(key, async (tx) => {
		const rows = (await tx.execute(sql`
			select statement from live_fact where predicate != ${OBSERVES}
			order by lower(recorded) desc limit ${settings.insightFactsK}
		`)) as unknown as { statement: string }[];
		return rows.map((row) => row.statement);
	});
	if (grounding.length < 2) return 0;
	const insight = await reflect(
		settings.insightSystem,
		'Facts:\n' + grounding.map((statement) => `- ${statement}`).join('\n')
	);
	const kept = keptObservations(insight);
	if (!kept.length) return 0;
	const vectors = await embed(kept.map((obs) => obs.statement), 'document');
	const nodeId = entityId(OBSERVATION_NODE, OBSERVATION);
	return asSystem(key, async (tx) => {
		await tx.execute(sql`
			insert into entity_content (id, name, type)
			values (${nodeId}, ${OBSERVATION_NODE}, ${OBSERVATION})
			on conflict do nothing
		`);
		await tx.execute(sql`
			insert into entity_claim (id, content_id, created_by, scopes)
			values (uuidv7(), ${nodeId}, ${settings.systemUserId}, ${uuidArray(key)}::uuid[])
			on conflict (content_id, scopes) do nothing
		`);
		let written = 0;
		for (const [index, obs] of kept.entries())
			written += (await writeObservation(tx, key, nodeId, obs, vectors[index])) ? 1 : 0;
		return written;
	});
};
