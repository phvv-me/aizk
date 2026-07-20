import { ApiClient } from '$lib/server/api';
import type { PageServerLoad } from './$types';

const allowed = new Set([7, 30, 90, 365]);

export const load: PageServerLoad = async ({ locals, url }) => {
  const requested = Number(url.searchParams.get('days') ?? 30);
  const days = allowed.has(requested) ? requested : 30;
  try {
    return { days, usage: await new ApiClient(locals.logtoClient).usage(days) };
  } catch {
    return { days, usage: null };
  }
};
