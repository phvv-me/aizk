export const appRoutes = {
  dashboard: '/app/dashboard',
  find: '/app/find',
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
  overview: '/app/admin/overview',
  queues: '/app/admin/queues',
  ingestion: '/app/admin/ingestion',
  storage: '/app/admin/storage',
  usage: '/app/admin/usage'
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
