import { describe, expect, it } from 'vitest';
import type { Me, Organization } from '../src/lib/api';
import { navigation } from '../src/lib/nav';

const organization = (over: Partial<Organization>): Organization => ({
  name: 'toshiba',
  description: '',
  roles: ['viewer'],
  permissions: ['read:memory'],
  writable: false,
  public: false,
  ...over
});

const me = (organizations: Organization[]): Me => ({ label: 'Pedro', organizations });

describe('navigation', () => {
  it('always offers the four base destinations to a signed-in user', () => {
    const links = navigation(me([])).flatMap((section) => section.links);
    expect(links.map((link) => link.href)).toEqual([
      '/dashboard',
      '/recall',
      '/sources',
      '/organizations'
    ]);
  });

  it('hides member management when no organization grants it', () => {
    const sections = navigation(me([organization({})]));
    expect(sections.map((section) => section.label)).not.toContain('Member management');
  });

  it('links member management only for organizations the caller may manage', () => {
    const sections = navigation(
      me([
        organization({ name: 'toshiba', permissions: ['manage:member'] }),
        organization({ name: 'family', permissions: ['delete:member'] }),
        organization({ name: 'public-reads', permissions: ['read:memory'] })
      ])
    );
    const management = sections.find((section) => section.label === 'Member management');
    expect(management?.links.map((link) => link.label)).toEqual(['toshiba', 'family']);
  });
});
