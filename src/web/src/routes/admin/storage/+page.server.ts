import { ApiClient } from '$lib/server/api';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals }) => {
  try {
    return { health: await new ApiClient(locals.logtoClient).adminHealth() };
  } catch {
    // The layout banner already explains the unreachable API; render empty states.
    return { health: null };
  }
};
