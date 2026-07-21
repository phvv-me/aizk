import { ApiClient } from '$lib/server/api';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals }) => {
  try {
    return { themes: await new ApiClient(locals.logtoClient).themes() };
  } catch {
    return { themes: null };
  }
};
