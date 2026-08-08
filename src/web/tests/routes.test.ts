import { describe, expect, it } from 'vitest';
import { adminHref, adminRoutes, appHref, appRoutes } from '../src/lib/routes';

describe('application routes', () => {
  it('keeps application paths under the authenticated prefix', () => {
    expect(Object.values(appRoutes).every((route) => route.startsWith('/app/'))).toBe(true);
  });

  it('builds encoded catalog links from canonical routes', () => {
    expect(appHref(appRoutes.sources, { search: 'Clean Code.pdf', origin: 'file' })).toBe(
      '/app/sources?search=Clean+Code.pdf&origin=file'
    );
    expect(appHref(appRoutes.subjects, { search: 'C++' })).toBe('/app/subjects?search=C%2B%2B');
  });
});

describe('operator console routes', () => {
  it('keeps operator paths inside the application shell that authenticates them', () => {
    expect(Object.values(adminRoutes).every((route) => route.startsWith('/app/admin/'))).toBe(true);
  });

  it('builds encoded usage filter links from the canonical usage route', () => {
    expect(adminHref(adminRoutes.usage, { operation: 'recall', actor_id: 'a b' })).toBe(
      '/app/admin/usage?operation=recall&actor_id=a+b'
    );
    expect(adminHref(adminRoutes.overview)).toBe('/app/admin/overview');
  });
});
