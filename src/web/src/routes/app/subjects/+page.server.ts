import { ApiClient, available } from '$lib/server/api';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, url }) => {
  const search = url.searchParams.get('search')?.trim() ?? '';
  const offset = Math.max(0, Number(url.searchParams.get('offset') ?? 0) || 0);
  const api = new ApiClient(locals.logtoClient);
  return {
    search,
    subjects: await available(() => api.subjects(search, 50, offset)),
    graph: await available(() => api.graph(24))
  };
};
