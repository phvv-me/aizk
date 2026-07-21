export type NavIcon =
  | 'dashboard'
  | 'recall'
  | 'sources'
  | 'findings'
  | 'subjects'
  | 'themes'
  | 'usage'
  | 'processing'
  | 'organizations';

export type NavLink = {
  label: string;
  href: string;
  icon: NavIcon;
};

export type NavSection = {
  label: string;
  links: NavLink[];
};

/** Build the fixed product information architecture. */
export function navigation(): NavSection[] {
  return [
    {
      label: 'Knowledge',
      links: [
        { label: 'Dashboard', href: '/app/dashboard', icon: 'dashboard' },
        { label: 'Recall', href: '/app/recall', icon: 'recall' }
      ]
    },
    {
      label: 'Explore',
      links: [
        { label: 'Sources', href: '/app/sources', icon: 'sources' },
        { label: 'Findings', href: '/app/findings', icon: 'findings' },
        { label: 'Subjects', href: '/app/subjects', icon: 'subjects' },
        { label: 'Themes', href: '/app/themes', icon: 'themes' }
      ]
    },
    {
      label: 'Operations',
      links: [
        { label: 'Usage', href: '/app/usage', icon: 'usage' },
        { label: 'Processing', href: '/app/processing', icon: 'processing' }
      ]
    },
    {
      label: 'Collaboration',
      links: [{ label: 'Organizations', href: '/app/organizations', icon: 'organizations' }]
    }
  ];
}
