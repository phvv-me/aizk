import { describe, expect, it } from 'vitest';
import { appHref, appRoutes } from '../src/lib/routes';

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
