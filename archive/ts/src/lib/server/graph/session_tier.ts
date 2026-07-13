// The working-memory tier promotion, the TS twin of graph/session_tier.py: read one exact
// scope's still-working session items oldest first, pick the aged and overflow ones, feed
// them through the ingestion seam, and stamp promoted_at so they are never offered again.
// The TS server has no extract/ingest pipeline yet, so the graph ingestion is an injected
// seam the caller supplies; the Python side's enqueue_pending follow-up belongs to its
// pgqueuer worker and has no TS counterpart.
import { sql } from 'drizzle-orm';

import { settings } from '../settings';
import { asSystem, scopeKey, uuidArray } from './system';

export interface WorkingItem {
	id: string;
	created_at: Date;
	kind: string;
	text: string;
	created_by: string;
	provenance: Record<string, unknown>;
}

export type IngestTexts = (items: WorkingItem[], scopes: string[]) => Promise<void>;

// Return aged and overflow items in their existing oldest-first order, the
// SessionItem.due_for_promotion twin.
export const dueForPromotion = (
	items: WorkingItem[],
	now: Date,
	ageMinutes: number,
	threshold: number
): WorkingItem[] => {
	const cutoff = now.getTime() - ageMinutes * 60_000;
	const overflow = Math.max(0, items.length - threshold);
	return items.filter((item, index) => item.created_at.getTime() <= cutoff || index < overflow);
};

// Feed a user's aged or overflow working items into the graph, return how many moved, the
// promote_sessions twin with the ingestion pipeline injected.
export const promoteSessions = async (
	ingest: IngestTexts,
	scopes?: string[]
): Promise<number> => {
	const key = scopeKey(scopes);
	const now = new Date();
	const rows = await asSystem(key, async (tx) => {
		return (await tx.execute(sql`
			select id, created_at, kind, text, created_by, provenance from session_item
			where promoted_at is null and scopes = ${uuidArray(key)}::uuid[]
			order by created_at
		`)) as unknown as (Omit<WorkingItem, 'created_at'> & { created_at: string | Date })[];
	});
	// The driver hands timestamptz back as text here, so the age math gets real dates.
	const items = rows.map((row) => ({ ...row, created_at: new Date(row.created_at) }));
	const due = dueForPromotion(
		items,
		now,
		settings.sessionPromoteAgeMinutes,
		settings.sessionPromoteThreshold
	);
	if (!due.length) return 0;
	await ingest(due, key);
	await asSystem(key, (tx) =>
		tx.execute(sql`
			update session_item set promoted_at = ${now.toISOString()}::timestamptz
			where id = any(${uuidArray(due.map((item) => item.id))}::uuid[])
		`)
	);
	return due.length;
};
