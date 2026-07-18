import { error } from '@sveltejs/kit';
import { settings } from '$lib/server/settings';
import type { RequestHandler } from './$types';

export const GET: RequestHandler = async ({ locals }) => {
  await locals.logtoClient.signIn({ redirectUri: settings.callbackUrl, firstScreen: 'register' });
  error(500, 'Logto sign-up did not redirect');
};
