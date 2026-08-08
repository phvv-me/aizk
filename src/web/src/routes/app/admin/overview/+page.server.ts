import { ApiClient, failure, reading } from '$lib/server/api';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals }) => {
  const api = new ApiClient(locals.logtoClient);
  const [health, hardware] = await Promise.all([
    reading(() => api.adminHealth()),
    reading(() => api.adminHardware())
  ]);
  return { health, hardware };
};

export const actions: Actions = {
  // A live retrieval, roughly 3 seconds in production, so it runs only on this explicit
  // request rather than blocking the page's own load.
  recall: async ({ locals }) => {
    try {
      return { recall: await new ApiClient(locals.logtoClient).adminRecall() };
    } catch (error) {
      return failure(error);
    }
  }
};
