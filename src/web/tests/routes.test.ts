import { describe, expect, it } from 'vitest';
import {
  adminHref,
  adminRoutes,
  appHref,
  appRoutes,
  homeFor,
  trustedOrigin
} from '../src/lib/routes';

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
  it('keeps operator paths under the dedicated admin prefix', () => {
    expect(Object.values(adminRoutes).every((route) => route.startsWith('/admin/'))).toBe(true);
  });

  it('builds encoded usage filter links from the canonical usage route', () => {
    expect(adminHref(adminRoutes.usage, { operation: 'recall', actor_id: 'a b' })).toBe(
      '/admin/usage?operation=recall&actor_id=a+b'
    );
    expect(adminHref(adminRoutes.overview)).toBe('/admin/overview');
  });
});

describe('origin resolution across the console and application hostnames', () => {
  const application = 'https://memory.example';
  const operator = 'https://console.example';
  const known = [application, operator];

  it('answers on an origin this deployment actually serves', () => {
    expect(trustedOrigin(application, known)).toBe(application);
    expect(trustedOrigin(operator, known)).toBe(operator);
  });

  it('refuses to mint a redirect for an origin it does not serve', () => {
    expect(trustedOrigin('https://attacker.example', known)).toBe(application);
    expect(trustedOrigin('', known)).toBe(application);
  });

  it('falls back to the application when no operator hostname is configured', () => {
    expect(trustedOrigin(operator, [application, ''])).toBe(application);
  });

  it('lands a completed sign-in on the face whose hostname was used', () => {
    expect(homeFor(operator, operator)).toBe(adminRoutes.overview);
    expect(homeFor(application, operator)).toBe(appRoutes.dashboard);
    expect(homeFor(operator, '')).toBe(appRoutes.dashboard);
  });
});
