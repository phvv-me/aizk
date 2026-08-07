import { LogtoClientError, LogtoError, LogtoRequestError, OidcError } from '@logto/sveltekit';
import { error, redirect } from '@sveltejs/kit';
import type { AdminLinks, Me } from '$lib/api';
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
  const api = new ApiClient(locals.logtoClient);
  // The operator role can only be proven by asking the API, so an unreachable API refuses
  // access here instead of degrading to an offline shell the way the caller-facing app does,
  // and the admin check runs inside the same try as the calls it gates so every failure,
  // including the 403 it raises itself, is translated by the one catch below.
  try {
    const me: Me = await api.me();
    if (!me.admin) error(403, 'Operator access required.');
    const links: AdminLinks = await api.adminLinks();
    return { me, accountUrl: settings.accountUrl, links };
  } catch (cause) {
    if (authBroken(cause)) redirect(302, '/auth/sign-in');
    if (cause instanceof ApiError) error(cause.status < 500 ? cause.status : 502, cause.message);
    throw cause;
  }
};
