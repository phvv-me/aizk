import { ApiClient, available } from '$lib/server/api';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals }) => {
  const api = new ApiClient(locals.logtoClient);
  return {
    overview: await available(() => api.overview()),
    processing: await available(() => api.processing()),
    usage: await available(() => api.usage(30))
  };
};
