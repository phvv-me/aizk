import { handleLogto } from '@logto/sveltekit';
import { isRedirect, redirect, type Handle } from '@sveltejs/kit';
import { settings } from '$lib/server/settings';

let logto: Handle | undefined;

/** Build the Logto handle on the first request so builds never need runtime env. */
export const handle: Handle = async (input) => {
  logto ??= handleLogto(
    {
      endpoint: settings.logtoEndpoint,
      appId: settings.clientId,
      appSecret: settings.clientSecret,
      // `control` is the one scope the aizk API requires on every resource token.
      // Organization standing rides in the custom `aizk_groups` claim, so the
      // urn:logto:scope organization scopes stay out, nothing consumes them here.
      scopes: ['openid', 'profile', 'email', 'offline_access', 'control'],
      resources: [settings.apiResource]
    },
    { encryptionKey: settings.sessionSecret },
    { signInCallback: '/auth/sign-in-callback' }
  );

  try {
    return await logto(input);
  } catch (cause) {
    // A completed sign-in always leaves the Logto hook redirecting to `/`, which the static
    // landing page owns and this server never sees. Send the caller to the application instead.
    const completedSignIn = input.event.url.pathname === '/auth/sign-in-callback';
    if (isRedirect(cause) && cause.location === '/' && completedSignIn) {
      redirect(302, '/app/dashboard');
    }
    throw cause;
  }
};
