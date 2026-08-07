import { error } from '@sveltejs/kit';
import { settings } from '$lib/server/settings';
import type { RequestHandler } from './$types';

// POST so SvelteKit's origin check applies and a cross-site link cannot end the session.
export const POST: RequestHandler = async ({ locals, url }) => {
  await locals.logtoClient.signOut(settings.originFor(url.origin));
  error(500, 'Logto sign-out did not redirect');
};
