import { error } from '@sveltejs/kit';
import { settings } from '$lib/server/settings';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async ({ locals, url }) => {
  await locals.logtoClient.signIn({ redirectUri: settings.callbackFor(url.origin) });
  error(500, 'Logto sign-in did not redirect');
};
