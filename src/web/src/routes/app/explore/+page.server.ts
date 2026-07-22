import { ApiClient } from '$lib/server/api';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals }) => {
  const api = new ApiClient(locals.logtoClient);
  const [overview, graph] = await Promise.allSettled([api.overview(), api.graph(40)]);
  return {
    overview: overview.status === 'fulfilled' ? overview.value : null,
    graph: graph.status === 'fulfilled' ? graph.value : null
  };
};
