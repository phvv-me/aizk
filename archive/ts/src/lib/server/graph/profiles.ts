// Entity profiles, the TS twin of graph/profiles.py: load the claimed entity roster and
// every related current fact in two RLS-filtered queries as the system identity, summarize
// each entity with the profile prompt, embed the batch, then upsert the whole generation
// in one app-role write keyed on (scopes, subject_id).
import { sql } from 'drizzle-orm';

import { embed, structured } from '../serving';
import { settings } from '../settings';
import { asSystem, scopeKey, uuidArray, type Reporter } from './system';

interface ProfileReport {
	summary: string;
}

const PROFILE_SCHEMA = {
	name: 'ProfileReport',
	schema: {
		type: 'object',
		properties: {
			summary: {
				type: 'string',
				description: 'one static-plus-dynamic paragraph grounded only in the facts'
			}
		},
		required: ['summary'],
		additionalProperties: false
	}
};

const report: Reporter<ProfileReport> = (system, user) =>
	structured<ProfileReport>(system, user, PROFILE_SCHEMA);

interface Grounding {
	subjectId: string;
	name: string;
	statements: string[];
}

// Load entity names and all related current fact statements in two queries, the
// ProfileBuilder.snapshot twin. Raises when a requested subject is not visible.
const snapshot = async (key: string[], subjectIds?: string[]): Promise<Grounding[]> =>
	asSystem(key, async (tx) => {
		const filter = subjectIds
			? sql` and ec.id = any(${uuidArray(subjectIds)}::uuid[])`
			: sql``;
		const roster = (await tx.execute(sql`
			select ec.id, ec.name from entity_content ec
			where ec.id in (select content_id from entity_claim)${filter}
			order by ec.id
		`)) as unknown as { id: string; name: string }[];
		const entities = new Map(roster.map((row) => [row.id, row.name]));
		if (subjectIds) {
			const missing = subjectIds.filter((id) => !entities.has(id)).sort();
			if (missing.length)
				throw new Error(`entities ${missing.join(', ')} are not visible to build profiles`);
		}
		const statements = new Map<string, string[]>();
		if (entities.size) {
			const ids = uuidArray([...entities.keys()]);
			const rows = (await tx.execute(sql`
				select subject_id, object_id, statement from live_fact
				where subject_id = any(${ids}::uuid[]) or object_id = any(${ids}::uuid[])
				order by lower(recorded), id
			`)) as unknown as { subject_id: string; object_id: string | null; statement: string }[];
			for (const row of rows) {
				statements.set(row.subject_id, [...(statements.get(row.subject_id) ?? []), row.statement]);
				if (row.object_id && entities.has(row.object_id) && row.object_id !== row.subject_id)
					statements.set(row.object_id, [...(statements.get(row.object_id) ?? []), row.statement]);
			}
		}
		return [...entities].map(([subjectId, name]) => ({
			subjectId,
			name,
			statements: statements.get(subjectId) ?? []
		}));
	});

interface Draft {
	subjectId: string;
	summary: string;
	vector: string;
}

// Summarize every grounding and embed all resulting profiles in one batch.
const summarize = async (
	groundings: Grounding[],
	summarizer: Reporter<ProfileReport>
): Promise<Draft[]> => {
	const reports: ProfileReport[] = [];
	for (const grounding of groundings)
		reports.push(
			await summarizer(
				settings.profileSystem,
				`Entity: ${grounding.name}\n\nFacts:\n` +
					grounding.statements.map((statement) => `- ${statement}`).join('\n')
			)
		);
	const vectors = await embed(reports.map((item) => item.summary), 'document');
	return groundings.map((grounding, index) => ({
		subjectId: grounding.subjectId,
		summary: reports[index].summary,
		vector: vectors[index]
	}));
};

// Upsert a complete profile batch and return profile ids keyed by subject.
const store = async (key: string[], drafts: Draft[]): Promise<Map<string, string>> => {
	if (!drafts.length) return new Map();
	const payload = JSON.stringify(
		drafts.map((draft) => ({
			subject_id: draft.subjectId,
			summary: draft.summary,
			vector: draft.vector
		}))
	);
	return asSystem(key, async (tx) => {
		const rows = (await tx.execute(sql`
			insert into profile (id, created_by, scopes, subject_id, summary, embedding)
			select uuidv7(), ${settings.systemUserId}, ${uuidArray(key)}::uuid[],
				d.subject_id, d.summary, (d.vector)::halfvec
			from jsonb_to_recordset(${payload}::jsonb) as d(subject_id uuid, summary text, vector text)
			on conflict (scopes, subject_id) do update
				set summary = excluded.summary, embedding = excluded.embedding
			returning subject_id, id
		`)) as unknown as { subject_id: string; id: string }[];
		return new Map(rows.map((row) => [row.subject_id, row.id]));
	});
};

// Rebuild one entity profile through short read and write transactions, the build_profile
// twin, returning the profile id.
export const buildProfile = async (
	subjectId: string,
	scopes?: string[],
	summarizer: Reporter<ProfileReport> = report
): Promise<string> => {
	const key = scopeKey(scopes);
	const groundings = await snapshot(key, [subjectId]);
	const drafts = await summarize(groundings, summarizer);
	const profileIds = await store(key, drafts);
	return profileIds.get(subjectId)!;
};

// Rebuild every visible profile with one snapshot and one bulk write, the refresh_profiles
// twin, returning the count.
export const refreshProfiles = async (
	scopes?: string[],
	summarizer: Reporter<ProfileReport> = report
): Promise<number> => {
	const key = scopeKey(scopes);
	const groundings = await snapshot(key);
	const drafts = await summarize(groundings, summarizer);
	await store(key, drafts);
	return drafts.length;
};
