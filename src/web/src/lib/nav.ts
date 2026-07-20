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
        { label: 'Dashboard', href: '/dashboard', icon: 'dashboard' },
        { label: 'Recall', href: '/recall', icon: 'recall' }
      ]
    },
    {
      label: 'Explore',
      links: [
        { label: 'Sources', href: '/sources', icon: 'sources' },
        { label: 'Findings', href: '/findings', icon: 'findings' },
        { label: 'Subjects', href: '/subjects', icon: 'subjects' },
        { label: 'Themes', href: '/themes', icon: 'themes' }
      ]
    },
    {
      label: 'Operations',
      links: [
        { label: 'Usage', href: '/usage', icon: 'usage' },
        { label: 'Processing', href: '/processing', icon: 'processing' }
      ]
    },
    {
      label: 'Collaboration',
      links: [{ label: 'Organizations', href: '/organizations', icon: 'organizations' }]
    }
  ];
}
