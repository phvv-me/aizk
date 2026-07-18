import type { Me } from './api';

export type NavIcon = 'dashboard' | 'recall' | 'sources' | 'organizations' | 'members';

export type NavLink = {
  label: string;
  href: string;
  icon: NavIcon;
};

export type NavSection = {
  label: string;
  links: NavLink[];
};

const managementPermissions = ['manage:member', 'delete:member'];

/** Build the sidebar sections a signed-in caller is allowed to see. */
export function navigation(me: Me): NavSection[] {
  const sections: NavSection[] = [
    {
      label: 'Knowledge',
      links: [
        { label: 'Dashboard', href: '/dashboard', icon: 'dashboard' },
        { label: 'Recall', href: '/recall', icon: 'recall' },
        { label: 'Sources', href: '/sources', icon: 'sources' }
      ]
    },
    {
      label: 'Collaboration',
      links: [{ label: 'Organizations', href: '/organizations', icon: 'organizations' }]
    }
  ];
  const managed = me.organizations.filter((organization) =>
    managementPermissions.some((permission) => organization.permissions.includes(permission))
  );
  if (managed.length > 0) {
    sections.push({
      label: 'Member management',
      links: managed.map((organization) => ({
        label: organization.name,
        href: `/organizations#${encodeURIComponent(organization.name)}`,
        icon: 'members'
      }))
    });
  }
  return sections;
}
