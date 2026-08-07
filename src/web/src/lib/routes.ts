export const appRoutes = {
  dashboard: '/app/dashboard',
  recall: '/app/recall',
  explore: '/app/explore',
  sources: '/app/sources',
  findings: '/app/findings',
  subjects: '/app/subjects',
  themes: '/app/themes',
  usage: '/app/usage',
  processing: '/app/processing',
  organizations: '/app/organizations'
} as const;

export type AppRoute = (typeof appRoutes)[keyof typeof appRoutes];

export const adminRoutes = {
  overview: '/admin/overview',
  queues: '/admin/queues',
  ingestion: '/admin/ingestion',
  storage: '/admin/storage',
  usage: '/admin/usage'
} as const;

export type AdminRoute = (typeof adminRoutes)[keyof typeof adminRoutes];

/** Add deterministic search parameters to one canonical route. */
function withQuery(route: string, parameters: Record<string, string | number | undefined>): string {
  const search = new URLSearchParams();
  Object.entries(parameters).forEach(([key, value]) => {
    if (value !== undefined && value !== '') search.set(key, String(value));
  });
  const query = search.toString();
  return query ? `${route}?${query}` : route;
}

/** Add deterministic search parameters to one canonical application route. */
export function appHref(
  route: AppRoute,
  parameters: Record<string, string | number | undefined> = {}
): string {
  return withQuery(route, parameters);
}

/** Add deterministic search parameters to one canonical operator console route. */
export function adminHref(
  route: AdminRoute,
  parameters: Record<string, string | number | undefined> = {}
): string {
  return withQuery(route, parameters);
}

/**
 * The origin a browser arriving on `origin` is sent back to, which is that origin only when
 * this deployment answers on it.
 *
 * Logto redeems an authorization code only against the exact redirect it was issued for, and
 * the console and the application answer on different hostnames. Echoing whatever origin
 * arrived would let any Host header mint a redirect, so an unrecognized one falls back to the
 * first known origin rather than being trusted.
 */
export function trustedOrigin(origin: string, known: readonly string[]): string {
  const answered = known.filter(Boolean);
  return answered.includes(origin) ? origin : answered[0];
}

/** Where a completed sign-in lands, which is the console when it owns the origin used. */
export function homeFor(origin: string, operator: string): string {
  return operator && origin === operator ? adminRoutes.overview : appRoutes.dashboard;
}
