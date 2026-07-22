import { describe, expect, it } from 'vitest';
import { navigation } from '../src/lib/nav';

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
