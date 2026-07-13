// The app-role database client and the caller-bound transaction every recall runs in.
// Authority lives in Postgres: `actingAs` binds the caller's scope table to the same
// `app.scopes` GUC the RLS policies read, so the SQL below never filters by user.
import { drizzle } from 'drizzle-orm/postgres-js';
import { sql } from 'drizzle-orm';
import postgres from 'postgres';

import { settings } from './settings';

export interface User {
	id: string;
	label?: string;
	read: string[];
	write: string[];
	public: string[];
}

export const client = postgres(settings.databaseUrl, {
	prepare: false,
	connection: { 'vchordrq.prefilter': 'on' }
});

export const db = drizzle(client);

// The owner-role client for structural maintenance, the twin of the Python engine's
// bypass_rls(): aizk_admin owns the schema and carries BYPASSRLS, so its transactions see
// and write every row with no app.scopes GUC. postgres-js connects lazily, so the pool
// stays empty until a graph pass actually runs.
const adminClient = postgres(settings.adminDatabaseUrl, { prepare: false });

export const adminDb = drizzle(adminClient);

export type Tx = Parameters<Parameters<typeof db.transaction>[0]>[0];

export const actingAs = async <T>(
	user: User,
	work: (tx: Parameters<Parameters<typeof db.transaction>[0]>[0]) => Promise<T>
): Promise<T> =>
	db.transaction(async (tx) => {
		const scopes = JSON.stringify({ read: user.read, write: user.write, public: user.public });
		await tx.execute(sql`select set_config('app.scopes', ${scopes}, true)`);
		return work(tx);
	});
