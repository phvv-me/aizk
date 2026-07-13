// Round-trip the pgqueuer-compatible queue client: enqueue a deduplicated test job,
// observe the dedupe refusal, dequeue it under a concurrency limit, complete it, and
// confirm the queue row is gone.
import { exit } from 'node:process';

import { client } from '../src/lib/server/db';
import { complete, dequeue, enqueue } from '../src/lib/server/worker/queue';

const ENTRYPOINT = 'aizk_ts_queue_check';

const main = async () => {
	const first = await enqueue(ENTRYPOINT, { probe: true }, 'queue-check');
	const second = await enqueue(ENTRYPOINT, { probe: true }, 'queue-check');
	const jobs = await dequeue(5, { [ENTRYPOINT]: 2 });
	for (const job of jobs) await complete(job.id, 'successful');
	const [remaining] = await client`
		select count(*)::int as n from pgqueuer where entrypoint = ${ENTRYPOINT}
	`;
	await client`delete from pgqueuer_log where entrypoint = ${ENTRYPOINT}`;
	console.log(
		JSON.stringify({
			first,
			second,
			dequeued: jobs.length,
			payload: jobs[0] ? JSON.parse(jobs[0].payload ?? 'null') : null,
			remaining: remaining.n
		})
	);
	exit(0);
};

main().catch((error) => {
	console.error(error);
	exit(1);
});
