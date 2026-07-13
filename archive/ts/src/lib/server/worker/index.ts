// The autonomous engine, schedule.py's run_worker as one long-lived node process: drain
// the shared pgqueuer queue and fire the scheduled passes. The on-write chain is the same
// as Python's — one durable job per pending chunk builds its graph slice, bumps the touched
// entities' dirty watermarks, and chains a debounced profile rebuild per entity — and every
// cron fans its task out across the stored scope roster as deduplicated per-scope jobs.
import { Cron } from 'croner';
import { sql } from 'drizzle-orm';

import { settings } from '../settings';
import { ingestTexts, type TextSource } from '../extract/ingest';
import { captureFrom } from '../extract/models';
import { ensureCurrent } from '../extract/ontology';
import { extractAndConsolidate, pendingChunks, type ChunkRow } from '../graph/build';
import { buildProfile } from '../graph/profiles';
import { promoteSessions } from '../graph/session_tier';
import { asSystem, scopeKey } from '../graph/system';
import { complete, dequeue, enqueue, onQueueChange, type Job } from './queue';
import { bumpWatermarks, scheduledTasks, scopeRoster, setWatermark, type ScheduledTask } from './tasks';

const EXTRACT_ENTRYPOINT = 'aizk_build_graph_chunk';
const PROFILE_ENTRYPOINT = 'aizk_build_profile';

interface ChunkJob {
	chunk_id: string;
	scopes: string[];
}

interface ProfileJob {
	entity_id: string;
	scopes: string[];
}

// Enqueue a durable extraction job for every pending chunk, deduplicated on the chunk.
export const enqueuePending = async (scopes?: string[], limit: number | null = null): Promise<number> => {
	const key = scopeKey(scopes);
	const chunks = await pendingChunks(key, limit, null);
	let queued = 0;
	for (const chunk of chunks)
		if (await enqueue(EXTRACT_ENTRYPOINT, { chunk_id: chunk.id, scopes: key }, chunk.id))
			queued += 1;
	return queued;
};

const enqueueProfiles = async (entityIds: string[], key: string[]): Promise<void> => {
	for (const entityId of entityIds)
		await enqueue(
			PROFILE_ENTRYPOINT,
			{ entity_id: entityId, scopes: key },
			`profile:${key.join(',')}:${entityId}`
		);
};

// Build one dequeued chunk's graph slice in its exact scope, bump the touched entities'
// dirty watermarks, and chain the debounced profile rebuilds.
const handleChunkJob = async (payload: ChunkJob): Promise<void> => {
	const key = scopeKey(payload.scopes);
	const [chunk] = await asSystem(
		key,
		async (tx) =>
			(await tx.execute(sql`
				select id, document_id, text, provenance, created_by, scopes
				from chunk where id = ${payload.chunk_id}
			`)) as unknown as ChunkRow[]
	);
	if (!chunk || scopeKey(chunk.scopes).join(',') !== key.join(',')) return;
	const touched = [...(await extractAndConsolidate(chunk))];
	if (touched.length)
		await asSystem(key, (tx) => bumpWatermarks(tx, key, 'entity_dirty', touched));
	if (settings.profileOnWrite) await enqueueProfiles(touched, key);
};

// Rebuild one touched entity's profile and clear its dirty watermark.
const handleProfileJob = async (payload: ProfileJob): Promise<void> => {
	const key = scopeKey(payload.scopes);
	await buildProfile(payload.entity_id, key);
	await asSystem(key, (tx) => setWatermark(tx, key, 'entity_dirty', 0, payload.entity_id));
};

// Session promotion feeds due working items through the real ingestion pipeline and then
// enqueues extraction for the chunks it created, the promote_sessions wiring in Python.
const promoteWorkingSessions = async (scopes: string[]): Promise<void> => {
	const key = scopeKey(scopes);
	await promoteSessions(async (items) => {
		const sources: TextSource[] = items.map((item) => ({
			text: item.text,
			kind: item.kind,
			created_by: item.created_by,
			scopes: key,
			capture: captureFrom(item.provenance)
		}));
		await ingestTexts(sources, {
			id: settings.systemUserId,
			read: key,
			write: key,
			public: []
		});
	}, key);
	await enqueuePending(key);
};

const tasks: ScheduledTask[] = [
	...scheduledTasks,
	{
		name: 'session_promote',
		expression: settings.sessionPromoteCron,
		enabled: settings.sessionPromoteEnabled,
		execute: promoteWorkingSessions
	}
];

const taskByEntrypoint = new Map(tasks.map((task) => [`aizk_task_${task.name}`, task]));

// Fan one task out across the stored scope roster as deduplicated per-scope jobs.
const fanOut = async (task: ScheduledTask): Promise<void> => {
	for (const key of await scopeRoster())
		await enqueue(`aizk_task_${task.name}`, { scopes: key }, `${task.name}:${key.join(',')}`);
};

const runJob = async (job: Job): Promise<void> => {
	const payload = JSON.parse(job.payload ?? '{}') as ChunkJob & ProfileJob & { scopes: string[] };
	if (job.entrypoint === EXTRACT_ENTRYPOINT) return handleChunkJob(payload);
	if (job.entrypoint === PROFILE_ENTRYPOINT) return handleProfileJob(payload);
	const task = taskByEntrypoint.get(job.entrypoint);
	if (!task) throw new Error(`unknown entrypoint ${job.entrypoint}`);
	return task.execute(payload.scopes);
};

// Drain every eligible job, batches racing per-entrypoint concurrency limits db-side.
export const drainOnce = async (batchSize = 10): Promise<number> => {
	const limits: Record<string, number> = {
		[EXTRACT_ENTRYPOINT]: settings.graphBuildConcurrency,
		[PROFILE_ENTRYPOINT]: 0,
		...Object.fromEntries([...taskByEntrypoint.keys()].map((name) => [name, 0]))
	};
	let processed = 0;
	for (;;) {
		const jobs = await dequeue(batchSize, limits);
		if (!jobs.length) return processed;
		await Promise.all(
			jobs.map(async (job) => {
				try {
					await runJob(job);
					await complete(job.id, 'successful');
				} catch (error) {
					console.error(`job ${job.id} (${job.entrypoint}) failed:`, error);
					await complete(job.id, 'exception');
				}
			})
		);
		processed += jobs.length;
	}
};

export const runWorker = async (): Promise<void> => {
	await asSystem(scopeKey(), (tx) => ensureCurrent(tx));
	for (const task of tasks)
		if (task.enabled) new Cron(task.expression, () => void fanOut(task));
	let draining = false;
	const drain = () => {
		if (draining) return;
		draining = true;
		void drainOnce()
			.catch((error) => console.error('drain failed:', error))
			.finally(() => {
				draining = false;
			});
	};
	await onQueueChange(drain);
	setInterval(drain, 30_000);
	drain();
	console.log('aizk worker (ts) listening on the queue and the scheduler');
};
