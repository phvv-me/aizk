// The scheduled maintenance passes and their fan-out, background/tasks.py's registry: each
// task fires on its own cron, fans out across the stored scope roster as one deduplicated
// queue job per exact scope set, and runs under the system identity for that set. The
// growth-gated rebuilds read the same fact-count watermarks the Python worker writes.
// Python-only tasks stay Python-owned and never register here: self_improve needs the eval
// harness and backup wraps pg_dump.
import { sql } from 'drizzle-orm';

import { settings } from '../settings';
import { buildCommunities } from '../graph/communities';
import { buildRaptor } from '../graph/raptor';
import { decay } from '../graph/decay';
import { dedupEntities } from '../graph/repair';
import { deriveInsights } from '../graph/insight';
import { refreshProfiles } from '../graph/profiles';
import { asSystem, bypassRls, scopeKey, uuidArray, type Tx } from '../graph/system';

export interface ScheduledTask {
	name: string;
	expression: string;
	enabled: boolean;
	execute: (scopes: string[]) => Promise<void>;
}

export const readWatermark = async (
	tx: Tx,
	key: string[],
	kind: string,
	ref = 'global'
): Promise<number> => {
	const rows = (await tx.execute(sql`
		select counter from watermark
		where scopes = ${uuidArray(key)}::uuid[] and kind = ${kind}::watermark_kind and ref = ${ref}
	`)) as { counter: number }[];
	return rows[0]?.counter ?? 0;
};

export const setWatermark = async (
	tx: Tx,
	key: string[],
	kind: string,
	counter: number,
	ref = 'global'
): Promise<void> => {
	await tx.execute(sql`
		insert into watermark (id, kind, ref, counter, payload, created_by, scopes)
		values (uuidv7(), ${kind}::watermark_kind, ${ref}, ${counter}, '{}'::jsonb,
			${settings.systemUserId}, ${uuidArray(key)}::uuid[])
		on conflict (scopes, kind, ref)
		do update set counter = excluded.counter, updated_at = now()
	`);
};

export const bumpWatermarks = async (
	tx: Tx,
	key: string[],
	kind: string,
	refs: string[]
): Promise<void> => {
	if (!refs.length) return;
	await tx.execute(sql`
		insert into watermark (id, kind, ref, counter, payload, created_by, scopes)
		select uuidv7(), ${kind}::watermark_kind, ref, 1, '{}'::jsonb,
			${settings.systemUserId}, ${uuidArray(key)}::uuid[]
		from unnest(${`{${refs.map((ref) => `"${ref}"`).join(',')}}`}::text[]) as ref
		on conflict (scopes, kind, ref)
		do update set counter = watermark.counter + excluded.counter, updated_at = now()
	`);
};

// Every fact claim ever recorded in one scope set, the monotonic growth signal.
const recordedFactCount = async (tx: Tx, key: string[]): Promise<number> => {
	const rows = (await tx.execute(sql`
		select count(*)::int as n from fact_claim where scopes = ${uuidArray(key)}::uuid[]
	`)) as { n: number }[];
	return rows[0].n;
};

// Run a growth-gated rebuild only once the graph grew by threshold facts since its watermark.
const runIfGrown = async (
	scopes: string[],
	kind: string,
	threshold: number,
	build: () => Promise<unknown>
): Promise<void> => {
	const key = scopeKey(scopes);
	const [current, last] = await asSystem(key, async (tx) => [
		await recordedFactCount(tx, key),
		await readWatermark(tx, key, kind)
	]);
	if (current - last < threshold) return;
	await build();
	await asSystem(key, (tx) => setWatermark(tx, key, kind, current));
};

export const scheduledTasks: ScheduledTask[] = [
	{
		name: 'decay',
		expression: settings.decayCron,
		enabled: settings.decayEnabled,
		execute: async (scopes) => {
			await decay(scopes, settings.decayHalfLifeDays);
		}
	},
	{
		name: 'dedup',
		expression: settings.dedupCron,
		enabled: settings.dedupEnabled,
		execute: async (scopes) => {
			await dedupEntities(scopes);
		}
	},
	{
		name: 'communities',
		expression: settings.communitiesCron,
		enabled: settings.communitiesEnabled,
		execute: (scopes) =>
			runIfGrown(scopes, 'fact_count', settings.communitiesEveryNFacts, () =>
				buildCommunities(scopes)
			)
	},
	{
		name: 'raptor',
		expression: settings.raptorCron,
		enabled: settings.raptorEnabled,
		execute: (scopes) =>
			runIfGrown(scopes, 'raptor_fact_count', settings.raptorEveryNFacts, () =>
				buildRaptor(scopes)
			)
	},
	{
		name: 'profile_refresh',
		expression: settings.profileRefreshCron,
		enabled: settings.profileRefreshEnabled,
		execute: async (scopes) => {
			await refreshProfiles(scopes);
		}
	},
	{
		name: 'insight',
		expression: settings.insightCron,
		enabled: settings.insightEnabled,
		execute: async (scopes) => {
			await deriveInsights(scopes);
		}
	}
];

// Every exact scope set with stored memory, read under the owner role past row security.
export const scopeRoster = async (): Promise<string[][]> => {
	const rows = await bypassRls(
		async (tx) =>
			(await tx.execute(sql`
				select scopes from document union select scopes from session_item
			`)) as { scopes: string[] }[]
	);
	const distinct = new Map<string, string[]>();
	for (const row of rows) {
		if (!row.scopes.length) continue;
		const key = scopeKey(row.scopes);
		distinct.set(key.join(','), key);
	}
	return [...distinct.values()].sort((a, b) => (a.join(',') < b.join(',') ? -1 : 1));
};
