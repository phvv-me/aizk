import { error } from '@sveltejs/kit';
import { settings } from '$lib/server/settings';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async ({ locals }) => {
  await locals.logtoClient.signIn({ redirectUri: settings.callbackUrl });
  error(500, 'Logto sign-in did not redirect');
};
