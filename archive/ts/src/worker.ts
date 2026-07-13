// The worker process entrypoint: chefe run worker.
import { runWorker } from './lib/server/worker';

runWorker().catch((error) => {
	console.error('worker failed to start:', error);
	process.exit(1);
});
