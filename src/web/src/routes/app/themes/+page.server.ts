import { ApiClient } from '$lib/server/api';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, url }) => {
  const offset = Math.max(0, Number(url.searchParams.get('offset') ?? 0) || 0);
  try {
    return { themes: await new ApiClient(locals.logtoClient).themes(50, offset) };
  } catch {
    return { themes: null };
  }
};
