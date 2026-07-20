import { describe, expect, it } from 'vitest';
import { navigation } from '../src/lib/nav';

describe('navigation', () => {
  it('offers the fixed product destinations in their information architecture', () => {
    const sections = navigation();
    expect(sections.map((section) => section.label)).toEqual([
      'Knowledge',
      'Explore',
      'Operations',
      'Collaboration'
    ]);
    expect(sections.flatMap((section) => section.links).map((link) => link.href)).toEqual([
      '/dashboard',
      '/recall',
      '/sources',
      '/findings',
      '/subjects',
      '/themes',
      '/usage',
      '/processing',
      '/organizations'
    ]);
  });

  it('keeps organization management inside Collaboration without sidebar duplication', () => {
    const sections = navigation();
    expect(sections.map((section) => section.label)).not.toContain('Member management');
    expect(sections.at(-1)?.links).toEqual([
      { label: 'Organizations', href: '/organizations', icon: 'organizations' }
    ]);
  });
});
