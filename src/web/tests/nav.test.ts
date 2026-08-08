import { describe, expect, it } from 'vitest';
import { adminNavigation, navigation } from '../src/lib/nav';

describe('navigation', () => {
  it('offers the fixed product destinations in their information architecture', () => {
    const sections = navigation();
    expect(sections.map((section) => section.label)).toEqual([
      'Knowledge',
      'Memory',
      'Operations',
      'Collaboration'
    ]);
    expect(sections.flatMap((section) => section.links).map((link) => link.href)).toEqual([
      '/app/dashboard',
      '/app/recall',
      '/app/explore',
      '/app/sources',
      '/app/findings',
      '/app/subjects',
      '/app/themes',
      '/app/usage',
      '/app/processing',
      '/app/organizations'
    ]);
  });

  it('keeps organization management inside Collaboration without sidebar duplication', () => {
    const sections = navigation();
    expect(sections.map((section) => section.label)).not.toContain('Member management');
    expect(sections.at(-1)?.links).toEqual([
      { label: 'Organizations', href: '/app/organizations', icon: 'organizations' }
    ]);
  });
});

describe('adminNavigation', () => {
  it('offers the fixed operator console destinations', () => {
    const sections = adminNavigation();
    expect(sections.map((section) => section.label)).toEqual(['Operator']);
    expect(sections.flatMap((section) => section.links).map((link) => link.href)).toEqual([
      '/app/admin/overview',
      '/app/admin/queues',
      '/app/admin/ingestion',
      '/app/admin/storage',
      '/app/admin/usage'
    ]);
  });
});

describe('one sidebar that grows operator tools', () => {
  it('offers no operator section to an ordinary caller', () => {
    expect(navigation().some((section) => section.label === 'Operator')).toBe(false);
    expect(navigation(false).some((section) => section.label === 'Operator')).toBe(false);
  });

  it('appends the operator section for a caller the API vouched for', () => {
    const sections = navigation(true);
    expect(sections.some((section) => section.label === 'Operator')).toBe(true);
    // the product sections still come first, so operator tools read as an addition
    expect(sections[0].label).toBe('Knowledge');
    expect(sections.at(-1)?.label).toBe('Operator');
  });
});
