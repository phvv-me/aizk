import { ApiClient } from '$lib/server/api';
import type { PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals, url }) => {
  const search = url.searchParams.get('search')?.trim() ?? '';
  const requestedOrigin = url.searchParams.get('origin');
  const origin =
    requestedOrigin === 'document' || requestedOrigin === 'file' ? requestedOrigin : 'all';
  const offset = Math.max(0, Number(url.searchParams.get('offset') ?? 0) || 0);
  try {
    return {
      search,
      origin,
      sources: await new ApiClient(locals.logtoClient).sources(search, origin, 50, offset)
    };
  } catch {
    return { search, origin, sources: null };
  }
};
