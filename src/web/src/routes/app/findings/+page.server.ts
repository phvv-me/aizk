import { ApiClient } from '$lib/server/api';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, url }) => {
  const search = url.searchParams.get('search')?.trim() ?? '';
  const offset = Math.max(0, Number(url.searchParams.get('offset') ?? 0) || 0);
  try {
    return {
      search,
      findings: await new ApiClient(locals.logtoClient).findings(search, 50, offset)
    };
  } catch {
    return { search, findings: null };
  }
};
