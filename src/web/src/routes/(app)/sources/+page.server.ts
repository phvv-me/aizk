import { fail } from '@sveltejs/kit';
import { ApiClient, failure } from '$lib/server/api';
import type { Actions, PageServerLoad } from './$types';

export const load: PageServerLoad = async ({ locals }) => {
  try {
    return { overview: await new ApiClient(locals.logtoClient).overview() };
  } catch {
    // The layout banner already explains the unreachable API; render empty states.
    return { overview: null };
  }
};

export const actions: Actions = {
  intake: async ({ request, locals }) => {
    const data = await request.formData();
    const uri = String(data.get('source_uri') ?? '').trim();
    if (!uri.startsWith('https://')) {
      return fail(400, { message: 'Enter an https:// link to a document or page.' });
    }
    try {
      await new ApiClient(locals.logtoClient).remember({ source_uri: uri, preserve_source: true });
      return { accepted: uri };
    } catch (error) {
      return failure(error);
    }
  }
};
