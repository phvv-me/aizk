// The one-command embedding-model migration, the TS twin of graph/reembed.py: re-read each
// embedded table's source text and overwrite its embedding column in batches, all under
// the owner role since content tables carry no update policy and community rows no update
// policy either. Scoped tables rewrite one exact scope set; content tables rewrite fully.
import { sql, type SQL } from 'drizzle-orm';
import { chunk as batched } from 'es-toolkit';

import { embed } from '../serving';
import { settings } from '../settings';
import { bypassRls, scopeKey, uuidArray, type Tx } from './system';

export interface EmbeddedTarget {
	table: SQL;
	source: SQL;
	scoped: boolean;
}

export const EMBEDDED_TARGETS: EmbeddedTarget[] = [
	{ table: sql.raw('chunk'), source: sql.raw('text'), scoped: true },
	{ table: sql.raw('community'), source: sql.raw('summary'), scoped: true },
	{ table: sql.raw('profile'), source: sql.raw('summary'), scoped: true },
	{ table: sql.raw('entity_content'), source: sql.raw('name'), scoped: false },
	{ table: sql.raw('fact_content'), source: sql.raw('statement'), scoped: false }
];

// Re-read one table's source text and overwrite its embedding column in batches, the
// rewrite_embeddings twin, returning how many rows were rewritten.
export const rewriteEmbeddings = async (
	tx: Tx,
	target: EmbeddedTarget,
	scopes?: string[]
): Promise<number> => {
	const filter = scopes ? sql` where scopes = ${uuidArray(scopes)}::uuid[]` : sql``;
	const rows = (await tx.execute(sql`
		select id, ${target.source} as source from ${target.table}${filter} order by id
	`)) as unknown as { id: string; source: string }[];
	for (const batch of batched(rows, settings.reembedBatch)) {
		const vectors = await embed(batch.map((row) => row.source), 'document');
		const payload = JSON.stringify(
			batch.map((row, index) => ({ id: row.id, embedding: vectors[index] }))
		);
		await tx.execute(sql`
			update ${target.table} t set embedding = (d.embedding)::halfvec
			from jsonb_to_recordset(${payload}::jsonb) as d(id uuid, embedding text)
			where t.id = d.id
		`);
	}
	return rows.length;
};

// Re-encode every stored embedding with the current embedder, the reembed twin: one
// owner-role transaction per table, the per-tenant tables filtered to the scope key.
export const reembed = async (scopes?: string[]): Promise<number> => {
	const key = scopeKey(scopes);
	let total = 0;
	for (const target of EMBEDDED_TARGETS)
		total += await bypassRls((tx) =>
			rewriteEmbeddings(tx, target, target.scoped ? key : undefined)
		);
	return total;
};
