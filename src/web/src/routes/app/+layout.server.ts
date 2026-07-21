import { LogtoClientError, LogtoError, LogtoRequestError, OidcError } from '@logto/sveltekit';
import { error, redirect } from '@sveltejs/kit';
import type { Me } from '$lib/api';
import { ApiClient, ApiError } from '$lib/server/api';
import { settings } from '$lib/server/settings';
import type { LayoutServerLoad } from './$types';

/** A failed token exchange or refresh, which only a fresh sign-in can repair. */
function authBroken(cause: unknown): boolean {
  return (
    cause instanceof LogtoError ||
    cause instanceof LogtoClientError ||
    cause instanceof LogtoRequestError ||
    cause instanceof OidcError ||
    (cause instanceof ApiError && cause.status === 401)
  );
}

export const load: LayoutServerLoad = async ({ locals }) => {
  const user = locals.user;
  if (!user) redirect(302, '/auth/sign-in');
  const accountUrl = settings.accountUrl;
  try {
    return { me: await new ApiClient(locals.logtoClient).me(), apiOnline: true, accountUrl };
  } catch (cause) {
    // Broken sessions restart sign-in and authorization denials surface as real errors.
    // Only an unreachable or failing API degrades into the offline shell below.
    if (authBroken(cause)) redirect(302, '/auth/sign-in');
    if (cause instanceof ApiError && cause.status < 500) error(cause.status, cause.message);
    const fallback: Me = { label: user.name ?? user.email ?? user.sub, organizations: [] };
    return { me: fallback, apiOnline: false, accountUrl };
  }
};
