import { ApiClient } from '$lib/server/api';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals }) => {
  try {
    return { overview: await new ApiClient(locals.logtoClient).overview() };
  } catch {
    // The layout banner already explains the unreachable API; render empty states.
    return { overview: null };
  }
};
