// The daily decay pass, the TS twin of graph/decay.py and FactClaim.archive_stale: close
// the live, in-valid claims whose exponential relevance (half-life over last access,
// boosted by access count) fell below the floor, stamping the decay moment into their
// attributes so default recall drops them while history keeps them queryable.
import { sql } from 'drizzle-orm';

import { settings } from '../settings';
import { asSystem, scopeKey, uuidArray } from './system';

// Archive the stale, rarely accessed latest claims so default recall drops them, return
// the count.
export const decay = async (scopes?: string[], halfLifeDays = 90.0): Promise<number> => {
	const key = scopeKey(scopes);
	const now = new Date().toISOString();
	const stamp = now.replace('Z', '+00:00');
	const archived = await asSystem(key, async (tx) => {
		return (await tx.execute(sql`
			update fact_claim set
				recorded = tstzrange(lower(recorded), ${now}::timestamptz),
				attributes = attributes || jsonb_build_object('decayed', ${stamp}::text)
			where upper_inf(recorded)
				and (valid is null or valid @> ${now}::timestamptz)
				and scopes = ${uuidArray(key)}::uuid[]
				and power(0.5,
					extract(epoch from ${now}::timestamptz - coalesce(last_accessed, lower(recorded)))
						/ 86400.0 / ${halfLifeDays}::float8) * (1 + access_count) < ${settings.decayFloor}::float8
			returning id
		`)) as unknown as { id: string }[];
	});
	return archived.length;
};
