import { ApiClient } from '$lib/server/api';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals }) => {
  const api = new ApiClient(locals.logtoClient);
  const [overview, processing, usage] = await Promise.allSettled([
    api.overview(),
    api.processing(),
    api.usage(30)
  ]);
  return {
    overview: overview.status === 'fulfilled' ? overview.value : null,
    processing: processing.status === 'fulfilled' ? processing.value : null,
    usage: usage.status === 'fulfilled' ? usage.value : null
  };
};
