import { ApiClient } from '$lib/server/api';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, url }) => {
  const search = url.searchParams.get('search')?.trim() ?? '';
  const offset = Math.max(0, Number(url.searchParams.get('offset') ?? 0) || 0);
  const api = new ApiClient(locals.logtoClient);
  const [subjects, graph] = await Promise.allSettled([
    api.subjects(search, 50, offset),
    api.graph(24)
  ]);
  return {
    search,
    subjects: subjects.status === 'fulfilled' ? subjects.value : null,
    graph: graph.status === 'fulfilled' ? graph.value : null
  };
};
