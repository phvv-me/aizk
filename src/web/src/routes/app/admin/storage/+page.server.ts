import { ApiClient, reading } from '$lib/server/api';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals }) => {
  const api = new ApiClient(locals.logtoClient);
  return { health: await reading(() => api.adminHealth()) };
};
