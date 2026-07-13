// Share visible documents into a wider scope set as provenance-linked copies, the TS twin
// of graph/promote.py: copy the document and its chunks into the target scopes, then claim
// the shared entity and fact content there through the same idempotent claim inserts.
// The steps run as sequential statements in one transaction, not one multi-CTE statement:
// chunk's read-through SELECT policy checks the parent document, and INSERT..RETURNING
// validates new rows against SELECT policies, so the document must land in an earlier
// statement to be visible to its chunks' returning clause.
// These stay raw SQL by necessity: drizzle's insert().select() requires selecting every
// table column in definition order, which the generated tsv column can never satisfy.
import { sql } from 'drizzle-orm';

import { actingAs, type User } from './db';

export const share = async (
	documentIds: string[],
	scopes: string[],
	user: User
): Promise<{ shared: number }> => {
	const target = `{${[...scopes].sort().join(',')}}`;
	let shared = 0;
	for (const sourceId of documentIds) {
		const copied = await actingAs(user, async (tx) => {
			const copies = (await tx.execute(sql`
				insert into document (id, kind, title, source_uri, content_hash, created_by, scopes, promoted_from)
				select uuidv7(), kind, title, source_uri, content_hash, ${user.id}, ${target}::uuid[], id
				from document source
				where source.id = ${sourceId}
					and not exists (
						select 1 from document copy
						where copy.promoted_from = source.id and copy.scopes = ${target}::uuid[]
					)
				returning id
			`)) as { id: string }[];
			if (!copies.length) return 0;
			const copyId = copies[0].id;
			await tx.execute(sql`
				insert into chunk (id, document_id, ord, text, lexical, tokens, provenance, embedding, processed_at, created_by, scopes)
				select uuidv7(), ${copyId}, c.ord, c.text, c.lexical, c.tokens, c.provenance,
					c.embedding, c.processed_at, ${user.id}, ${target}::uuid[]
				from chunk c where c.document_id = ${sourceId}
			`);
			await tx.execute(sql`
				insert into entity_claim (id, content_id, created_by, scopes)
				select uuidv7(), entity_id, ${user.id}, ${target}::uuid[]
				from (
					select lf.subject_id as entity_id from live_fact lf
					join chunk c on c.id = lf.source_chunk_id where c.document_id = ${sourceId}
					union
					select lf.object_id from live_fact lf
					join chunk c on c.id = lf.source_chunk_id
					where c.document_id = ${sourceId} and lf.object_id is not null
				) touched
				on conflict (content_id, scopes) do nothing
			`);
			await tx.execute(sql`
				insert into fact_claim (id, content_id, valid, source_chunk_id, attributes, perspective_key, promoted_from, created_by, scopes)
				select uuidv7(), lf.content_id, lf.valid, copy_chunk.id, lf.attributes, lf.perspective_key,
					lf.id, ${user.id}, ${target}::uuid[]
				from live_fact lf
				join chunk source_chunk on source_chunk.id = lf.source_chunk_id
				join chunk copy_chunk on copy_chunk.document_id = ${copyId} and copy_chunk.ord = source_chunk.ord
				where source_chunk.document_id = ${sourceId}
				on conflict (content_id, scopes, perspective_key) where upper_inf(recorded) do nothing
			`);
			return 1;
		});
		shared += copied;
	}
	return { shared };
};
