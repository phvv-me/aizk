import { handleLogto } from '@logto/sveltekit';
import type { Handle } from '@sveltejs/kit';
import { settings } from '$lib/server/settings';

let logto: Handle | undefined;

/** Build the Logto handle on the first request so builds never need runtime env. */
export const handle: Handle = (input) => {
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
  return logto(input);
};
