// The write verbs' storage path: working-memory items and reference documents, the TS
// twins of extract/ingest.py's remember_session and record_reference. Chunking here is a
// plain paragraph packer, not the Python code-aware chunker; references are single-span so
// the simplification only matters once full document ingestion moves over.
import { createHash } from 'node:crypto';

import { and, eq, sql } from 'drizzle-orm';
import { v7 as uuid7 } from 'uuid';

import { actingAs, type User } from './db';
import { chunk, document, sessionItem } from './db/schema';
import { embed } from './serving';

export const remember = async (
	text: string,
	user: User,
	kind = 'note'
): Promise<{ id: string }> => {
	const [embedding] = await embed([text]);
	const id = uuid7();
	await actingAs(user, (tx) =>
		tx.insert(sessionItem).values({
			id,
			kind,
			text,
			embedding,
			provenance: user.label ? { speaker_label: user.label } : {},
			createdBy: user.id,
			scopes: user.write
		})
	);
	return { id };
};

export const reference = async (uri: string, user: User): Promise<{ id: string }> => {
	const [embedding] = await embed([uri]);
	const contentHash = createHash('sha256').update(uri).digest('hex');
	const documentId = uuid7();
	return actingAs(user, async (tx) => {
		await tx
			.insert(document)
			.values({
				id: documentId,
				kind: 'reference',
				title: uri,
				sourceUri: uri,
				contentHash,
				createdBy: user.id,
				scopes: user.write
			})
			.onConflictDoNothing();
		const [existing] = await tx
			.select({ id: document.id })
			.from(document)
			.where(and(eq(document.sourceUri, uri), eq(document.scopes, user.write)));
		if (existing.id !== documentId) return { id: existing.id };
		await tx.insert(chunk).values({
			id: uuid7(),
			documentId,
			ord: 0,
			text: uri,
			embedding,
			processedAt: sql`now()`,
			createdBy: user.id,
			scopes: user.write
		});
		return { id: documentId };
	});
};
