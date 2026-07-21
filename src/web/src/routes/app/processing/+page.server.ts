import { ApiClient } from '$lib/server/api';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals }) => {
  try {
    return { processing: await new ApiClient(locals.logtoClient).processing() };
  } catch {
    return { processing: null };
  }
};
