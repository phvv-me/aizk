// A pgqueuer-compatible queue client: the same tables, dequeue CTE, dedupe semantics, and
// completion log the Python worker's pgqueuer library uses, so either engine can enqueue
// and either engine's worker can drain one shared queue. Wakeups ride the ch_pgqueuer
// NOTIFY channel pgqueuer's own trigger emits on every queue-table change.
import { randomUUID } from 'node:crypto';

import { client } from '../db';

export interface Job {
	id: number;
	entrypoint: string;
	payload: string | null;
	priority: number;
}

export type JobOutcome = 'successful' | 'exception' | 'canceled';

export const queueManagerId = randomUUID();

// Enqueue one deduplicated job; false means the dedupe key is already queued or picked.
export const enqueue = async (
	entrypoint: string,
	payload: object,
	dedupeKey: string
): Promise<boolean> => {
	try {
		await client`
			with inserted as (
				insert into pgqueuer (priority, entrypoint, payload, execute_after, dedupe_key, headers, status)
				values (0, ${entrypoint}, ${Buffer.from(JSON.stringify(payload))}, now(), ${dedupeKey}, null, 'queued')
				returning id, entrypoint, status, priority
			)
			insert into pgqueuer_log (job_id, status, entrypoint, priority)
			select id, 'queued', entrypoint, priority from inserted
		`;
	} catch (error) {
		if ((error as { code?: string }).code === '23505') return false;
		throw error;
	}
	return true;
};

// The pgqueuer dequeue program: claim queued work per-entrypoint under its concurrency
// limit, recover stale picked jobs past the heartbeat timeout, log the pick.
export const dequeue = async (
	batchSize: number,
	entrypoints: Record<string, number>,
	heartbeatTimeout = '30 seconds'
): Promise<Job[]> => {
	const names = Object.keys(entrypoints);
	const limits = names.map((name) => entrypoints[name]);
	const rows = await client`
		with
		params as (
			select unnest(${names}::text[]) as entrypoint,
				unnest(${limits}::bigint[]) as concurrency_limit
		),
		picked as (
			select entrypoint, count(*) as total from pgqueuer
			where queue_manager_id is not null and entrypoint = any(${names})
			group by entrypoint
		),
		available as (
			select p.entrypoint from params p
			left join picked pk on pk.entrypoint = p.entrypoint
			where p.concurrency_limit <= 0 or coalesce(pk.total, 0) < p.concurrency_limit
		),
		next_queued as (
			select q.id from available a
			cross join lateral (
				select q2.id, q2.priority from pgqueuer q2
				where q2.entrypoint = a.entrypoint and q2.status = 'queued' and q2.execute_after < now()
				order by q2.priority desc, q2.id asc limit ${batchSize}
				for update skip locked
			) q
			order by q.priority desc, q.id asc limit ${batchSize}
		),
		next_stale as (
			select q.id from pgqueuer q
			join params p on p.entrypoint = q.entrypoint
			where q.status = 'picked' and q.heartbeat < now() - ${heartbeatTimeout}::interval
				and q.execute_after < now()
			order by q.priority desc, q.id asc
			for update skip locked limit ${batchSize}
		),
		eligible as (
			select id from (
				select id, 0 as src from next_queued
				union all
				select id, 1 as src from next_stale
			) combined order by src, id limit ${batchSize}
		),
		claimed as (
			update pgqueuer
			set status = 'picked', updated = now(), heartbeat = now(), queue_manager_id = ${queueManagerId}
			where id in (select id from eligible)
			returning *
		),
		log_pick as (
			insert into pgqueuer_log (job_id, status, entrypoint, priority)
			select id, status, entrypoint, priority from claimed
		)
		select id, entrypoint, convert_from(payload, 'utf8') as payload, priority
		from claimed order by priority desc, id asc
	`;
	return rows as unknown as Job[];
};

// Resolve finished jobs the way pgqueuer's log_job does: delete completed rows, hold
// failed ones for retry inspection, and append the outcome to the log.
export const complete = async (jobId: number, status: JobOutcome): Promise<void> => {
	await client`
		with job_status as (
			select ${jobId}::bigint as id, ${status}::pgqueuer_status as status, null::jsonb as traceback
		), deleted as (
			delete from pgqueuer
			where id = any(select js.id from job_status js where js.status != 'failed')
			returning id, entrypoint, priority
		), merged as (
			select job_status.id, job_status.status, job_status.traceback, deleted.entrypoint, deleted.priority
			from job_status join deleted on deleted.id = job_status.id
		)
		insert into pgqueuer_log (job_id, status, entrypoint, priority, traceback)
		select id, status, entrypoint, priority, traceback from merged
	`;
};

export const heartbeat = async (jobIds: number[]): Promise<void> => {
	if (!jobIds.length) return;
	await client`update pgqueuer set heartbeat = now() where id = any(${jobIds}::bigint[])`;
};

// Wake the drain loop on queue-table changes through pgqueuer's own notify trigger.
export const onQueueChange = async (wake: () => void): Promise<() => void> => {
	const { unlisten } = await client.listen('ch_pgqueuer', wake);
	return unlisten;
};
