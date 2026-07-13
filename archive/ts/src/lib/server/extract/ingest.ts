// The full document ingestion path ported from extract/ingest.py: batch text sources
// through one chunk preparation and embedding pipeline, find standing documents per exact
// scope set, skip unchanged content by hash, and refresh a changed document in place by
// retracting its derived fact claims and replacing its chunks under the stable document
// identity. Chunking goes through the /chunk sidecar seam (serving.chunkSpans) instead of
// Python's in-process chonkie; the filesystem walking of ingest_path is reduced to a thin
// single-file wrapper with no code/text sniffing.
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { basename, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';

import { sql, type SQL } from 'drizzle-orm';
import { v7 as uuid7 } from 'uuid';

import { actingAs, type Tx, type User } from '../db';
import { chunk as chunkTable, document as documentTable } from '../db/schema';
import { uuidArray } from '../graph/system';
import { chunkSpans, embed } from '../serving';
import { settings } from '../settings';
import { captureRecord, searchText, type CaptureContext } from './models';

// Digest the source text so re-ingesting identical content is a no-op.
export const contentHash = (text: string): string =>
	createHash('sha256').update(text, 'utf8').digest('hex');

// The lexical-lane text for a chunk, enriched with document and speaker context.
export const contextualLexical = (
	title: string,
	text: string,
	capture: CaptureContext | null
): string | null => {
	const preamble = settings.contextualBm25 ? title.trim() : '';
	const searchable = capture !== null ? searchText(capture, text) : text;
	if (!preamble && searchable === text) return null;
	return [preamble, searchable].filter(Boolean).join('\n');
};

// One text source ready for batched chunking, embedding, and storage.
export interface TextSource {
	text: string;
	title?: string | null;
	kind?: string;
	sourceUri?: string | null;
	createdBy?: string | null;
	scopes?: string[];
	capture?: CaptureContext | null;
	processed?: boolean;
}

// A nonempty text source after identity, chunk, and search text preparation.
export interface PreparedText {
	source: TextSource;
	title: string;
	digest: string;
	createdBy: string;
	scopes: string[];
	spans: string[];
	searchable: string[];
}

interface DocumentRow {
	id: string;
	source_uri: string | null;
	content_hash: string;
	scopes: string[];
	created_by: string;
}

interface MappedDocument {
	row: typeof documentTable.$inferInsert;
	chunks: (typeof chunkTable.$inferInsert)[];
}

// Close live fact claims derived from documents before their chunks change, the twin of
// FactClaim.retract_from_documents: recorded closes at now and the reason lands as an
// attribute stamped with the retraction instant.
export const retractFromDocuments = async (
	tx: Tx,
	documentIds: string[],
	reason: string
): Promise<string[]> => {
	const now = new Date().toISOString();
	const rows = (await tx.execute(sql`
		update fact_claim
		set recorded = tstzrange(lower(recorded), ${now}::timestamptz),
			attributes = attributes || jsonb_build_object(${reason}::text, ${now}::text)
		where upper_inf(recorded)
			and source_chunk_id in (
				select id from chunk where document_id in (
					select (value)::uuid from jsonb_array_elements_text(${JSON.stringify(documentIds)}::jsonb)
				)
			)
		returning id
	`)) as unknown as { id: string }[];
	return rows.map((row) => row.id);
};

// Store and refresh documents inside one caller-owned transaction.
export class DocumentStore {
	readonly tx: Tx;

	constructor(tx: Tx) {
		this.tx = tx;
	}

	// Find every standing document for a prepared batch in one query.
	async find(plans: PreparedText[]): Promise<(DocumentRow | null)[]> {
		if (!plans.length) return [];
		const conditions = plans.map((plan) =>
			plan.source.sourceUri != null
				? sql`(source_uri = ${plan.source.sourceUri} and scopes = ${uuidArray(plan.scopes)}::uuid[])`
				: sql`(content_hash = ${plan.digest} and scopes = ${uuidArray(plan.scopes)}::uuid[])`
		);
		const rows = (await this.tx.execute(sql`
			select id, source_uri, content_hash, scopes, created_by from document
			where ${sql.join(conditions, sql` or `)}
		`)) as unknown as DocumentRow[];
		const scopesKey = (scopes: string[]): string => [...scopes].sort().join(',');
		const bySource = new Map(
			rows
				.filter((row) => row.source_uri !== null)
				.map((row) => [`${row.source_uri}|${scopesKey(row.scopes)}`, row])
		);
		const byHash = new Map(rows.map((row) => [`${row.content_hash}|${scopesKey(row.scopes)}`, row]));
		return plans.map(
			(plan) =>
				(plan.source.sourceUri != null
					? bySource.get(`${plan.source.sourceUri}|${scopesKey(plan.scopes)}`)
					: byHash.get(`${plan.digest}|${scopesKey(plan.scopes)}`)) ?? null
		);
	}

	// Dedupe-check then store or refresh a document in its exact scope.
	async store(dedupe: SQL, mapped: MappedDocument): Promise<[string, boolean]> {
		const [existing] = (await this.tx.execute(sql`
			select id, source_uri, content_hash, scopes, created_by from document
			where ${dedupe} and scopes = ${uuidArray(mapped.row.scopes as string[])}::uuid[]
		`)) as unknown as DocumentRow[];
		if (existing !== undefined) {
			if (existing.content_hash === mapped.row.contentHash) return [existing.id, false];
			return [await this.refresh(existing, mapped), true];
		}
		await this.tx.insert(documentTable).values(mapped.row);
		if (mapped.chunks.length) await this.tx.insert(chunkTable).values(mapped.chunks);
		return [mapped.row.id as string, true];
	}

	// Replace a changed document's chunks while retaining its stable identity.
	async refresh(stale: DocumentRow, mapped: MappedDocument): Promise<string> {
		await this.tx.execute(sql`
			update document
			set title = ${mapped.row.title ?? null}, kind = ${mapped.row.kind},
				content_hash = ${mapped.row.contentHash}, updated_at = now()
			where id = ${stale.id}::uuid
		`);
		await retractFromDocuments(this.tx, [stale.id], 'source_refreshed');
		await this.tx.execute(sql`delete from chunk where document_id = ${stale.id}::uuid`);
		const replacements = mapped.chunks.map((span) => ({
			...span,
			documentId: stale.id,
			createdBy: stale.created_by,
			scopes: [...stale.scopes].sort()
		}));
		if (replacements.length) await this.tx.insert(chunkTable).values(replacements);
		return stale.id;
	}
}

// Batch text ingestion so many messages share the embedder's efficient request batches.
export class TextIngestor {
	// Resolve one source and return its nonempty chunk plan, or null for blank text.
	async prepare(source: TextSource): Promise<PreparedText | null> {
		const spans = await chunkSpans(source.text, source.kind ?? 'note');
		if (!spans.length) return null;
		const createdBy = source.createdBy ?? settings.systemUserId;
		const title =
			source.title || source.text.split(/\s+/u).filter(Boolean).slice(0, 8).join(' ');
		const capture = source.capture ?? null;
		return {
			source,
			title,
			digest: contentHash(source.text),
			createdBy,
			scopes: [...new Set(source.scopes?.length ? source.scopes : [createdBy])].sort(),
			spans,
			searchable: spans.map((span) => (capture !== null ? searchText(capture, span) : span))
		};
	}

	// Ingest sources in order after removing unchanged documents before embedding.
	async ingestMany(sources: TextSource[], user: User): Promise<[string | null, boolean][]> {
		const plans: (PreparedText | null)[] = [];
		for (const source of sources) plans.push(await this.prepare(source));
		const prepared = plans.filter((plan): plan is PreparedText => plan !== null);
		const found = await actingAs(user, (tx) => new DocumentStore(tx).find(prepared));
		const existing = found[Symbol.iterator]();
		const standing = plans.map((plan) =>
			plan !== null ? ((existing.next().value as DocumentRow | null) ?? null) : null
		);
		const pending = plans.filter(
			(plan, index): plan is PreparedText =>
				plan !== null &&
				(standing[index] === null || standing[index].content_hash !== plan.digest)
		);
		const searchable = pending.flatMap((plan) => plan.searchable);
		const vectors = searchable.length ? await embed(searchable, 'document') : [];
		let offset = 0;
		return actingAs(user, async (tx) => {
			const store = new DocumentStore(tx);
			const results: [string | null, boolean][] = [];
			for (let index = 0; index < plans.length; index += 1) {
				const plan = plans[index];
				const document = standing[index];
				if (plan === null) {
					results.push([null, false]);
					continue;
				}
				if (document !== null && document.content_hash === plan.digest) {
					results.push([document.id, false]);
					continue;
				}
				const embeddings = vectors.slice(offset, offset + plan.spans.length);
				offset += plan.spans.length;
				const dedupe =
					plan.source.sourceUri != null
						? sql`source_uri = ${plan.source.sourceUri}`
						: sql`content_hash = ${plan.digest}`;
				const [documentId, created] = await store.store(dedupe, this.document(plan, embeddings));
				console.info(`resolved document ${documentId} kind=${plan.source.kind ?? 'note'}`);
				results.push([documentId, created]);
			}
			return results;
		});
	}

	// Ingest one source through the same batching path used for a corpus.
	async ingest(source: TextSource, user: User): Promise<[string | null, boolean]> {
		return (await this.ingestMany([source], user))[0];
	}

	// Build the mapped document and chunk rows for one prepared source.
	document(plan: PreparedText, embeddings: string[]): MappedDocument {
		const capture = plan.source.capture ?? null;
		const documentId = uuid7();
		return {
			row: {
				id: documentId,
				kind: plan.source.kind ?? 'note',
				title: plan.title,
				sourceUri: plan.source.sourceUri ?? null,
				contentHash: plan.digest,
				createdBy: plan.createdBy,
				scopes: plan.scopes
			},
			chunks: plan.spans.map((span, order) => ({
				id: uuid7(),
				documentId,
				ord: order,
				text: span,
				lexical: contextualLexical(plan.title, span, capture),
				provenance: capture !== null ? captureRecord(capture) : {},
				embedding: embeddings[order],
				processedAt: plan.source.processed ? new Date() : null,
				createdBy: plan.createdBy,
				scopes: plan.scopes
			}))
		};
	}
}

// Store a raw text blob as a document with embedded chunks and return its id.
export const ingestText = async (
	text: string,
	user: User,
	options: {
		title?: string | null;
		kind?: string;
		sourceUri?: string | null;
		createdBy?: string | null;
		scopes?: string[];
		capture?: CaptureContext | null;
	} = {}
): Promise<string | null> => {
	const [documentId] = await new TextIngestor().ingest({ text, ...options }, user);
	return documentId;
};

// Batch a corpus of text sources through one chunk preparation and embedding pipeline.
export const ingestTexts = async (sources: TextSource[], user: User): Promise<(string | null)[]> =>
	(await new TextIngestor().ingestMany(sources, user)).map(([documentId]) => documentId);

// Ingest one file as a note document, the thin single-file remnant of Python's
// directory-walking ingest_path (no text sniffing, no code chunking).
export const ingestPath = async (
	path: string,
	user: User,
	options: { createdBy?: string | null; scopes?: string[] } = {}
): Promise<string | null> => {
	const absolute = resolve(path);
	return ingestText(readFileSync(absolute, 'utf8'), user, {
		...options,
		title: basename(absolute).replace(/\.[^.]*$/, ''),
		sourceUri: pathToFileURL(absolute).href
	});
};
