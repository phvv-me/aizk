import { json } from '@sveltejs/kit';
import type { RequestHandler } from './$types';
import { ApiClient } from '$lib/server/api';

export const GET: RequestHandler = async ({ locals }) => {
  if (!locals.user) return json({ message: 'sign in is required' }, { status: 401 });

  try {
    return json(await new ApiClient(locals.logtoClient).processing(), {
      headers: { 'Cache-Control': 'no-store' }
    });
  } catch {
    return json({ message: 'processing updates are unavailable' }, { status: 502 });
  }
};
