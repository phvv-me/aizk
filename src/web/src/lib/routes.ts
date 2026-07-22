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

/** Add deterministic search parameters to one canonical application route. */
export function appHref(
  route: AppRoute,
  parameters: Record<string, string | number | undefined> = {}
): string {
  const search = new URLSearchParams();
  Object.entries(parameters).forEach(([key, value]) => {
    if (value !== undefined && value !== '') search.set(key, String(value));
  });
  const query = search.toString();
  return query ? `${route}?${query}` : route;
}
