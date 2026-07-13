// Smoke the autonomous engine end to end on a scratch scope: ingest one document, enqueue
// its pending chunks, drain the queue, and assert the on-write chain ran — chunk processed,
// facts and entities written, dirty watermarks bumped then cleared by the chained profile
// rebuilds, queue left empty.
import { exit } from 'node:process';

import { sql } from 'drizzle-orm';

import { actingAs } from '../src/lib/server/db';
import { ingestText } from '../src/lib/server/extract/ingest';
import { ensureCurrent } from '../src/lib/server/extract/ontology';
import { asSystem, scopeKey } from '../src/lib/server/graph/system';
import { drainOnce, enqueuePending } from '../src/lib/server/worker';

const SCOPE = '00000000-0000-0000-0000-00000000c0c0';
const user = { id: SCOPE, read: [SCOPE], write: [SCOPE], public: [] };

const main = async () => {
	await asSystem(scopeKey(), (tx) => ensureCurrent(tx));
	const documentId = await ingestText(
		'The worker check service depends on the queue subsystem and drains every durable job the moment the notify channel wakes it.\n\nThe drain loop observes the notify channel closely and completes each dequeued job before it asks the queue for the next batch of work.',
		user,
		{ title: 'worker check note', createdBy: SCOPE, scopes: [SCOPE] }
	);
	const queued = await enqueuePending([SCOPE]);
	const processed = await drainOnce();
	// Counts run under the caller's own standing: the raw client has no app.scopes GUC and
	// row security correctly returns nothing for it.
	const [state] = await actingAs(
		user,
		async (tx) =>
			(await tx.execute(sql`
				select
					(select count(*)::int from chunk c join document d on d.id = c.document_id
						where d.scopes = ${`{${SCOPE}}`}::uuid[] and c.processed_at is null) as pending,
					(select count(*)::int from fact_claim where scopes = ${`{${SCOPE}}`}::uuid[]) as facts,
					(select count(*)::int from entity_claim where scopes = ${`{${SCOPE}}`}::uuid[]) as entities,
					(select count(*)::int from profile where scopes = ${`{${SCOPE}}`}::uuid[]) as profiles,
					(select coalesce(sum(counter), 0)::int from watermark
						where scopes = ${`{${SCOPE}}`}::uuid[] and kind = 'entity_dirty') as dirty,
					(select count(*)::int from pgqueuer) as queue
			`)) as Record<string, number>[]
	);
	console.log(JSON.stringify({ documentId: Boolean(documentId), queued, processed, ...state }));
	exit(0);
};

main().catch((error) => {
	console.error(error);
	exit(1);
});
