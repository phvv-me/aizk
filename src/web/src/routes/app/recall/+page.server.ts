import { fail } from '@sveltejs/kit';
import { ApiClient, failure } from '$lib/server/api';
import type { Actions } from './$types';

export const actions: Actions = {
  default: async ({ request, locals }) => {
    const query = String((await request.formData()).get('query') ?? '').trim();
    if (!query) return fail(400, { message: 'Ask a question first.' });
    try {
      const answer = await new ApiClient(locals.logtoClient).recall(query);
      return { query, markdown: answer.markdown };
    } catch (error) {
      return failure(error);
    }
  }
};
