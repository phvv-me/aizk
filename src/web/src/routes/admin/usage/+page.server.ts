import type { Operation } from '$lib/api';
import { enumParam, stringParam } from '$lib/query';
import { ApiClient, type UsageFilter } from '$lib/server/api';
import type { PageServerLoad } from './$types';

const OPERATIONS: readonly Operation[] = [
  'recall',
  'remember_text',
  'remember_file',
  'share',
  'artifact_read',
  'web_search',
  'web_fetch'
];

export const load: PageServerLoad = async ({ locals, url }) => {
  const filter: UsageFilter = {
    operation: enumParam(url.searchParams.get('operation'), OPERATIONS),
    actorId: stringParam(url.searchParams.get('actor_id')),
    scopeId: stringParam(url.searchParams.get('scope_id')),
    start: stringParam(url.searchParams.get('start')),
    end: stringParam(url.searchParams.get('end'))
  };
  try {
    return { usage: await new ApiClient(locals.logtoClient).adminUsage(filter), filter };
  } catch {
    // The layout banner already explains the unreachable API; render empty states.
    return { usage: null, filter };
  }
};
