import { appRoutes } from './routes';

export type NavIcon =
  | 'dashboard'
  | 'recall'
  | 'explore'
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
        { label: 'Dashboard', href: appRoutes.dashboard, icon: 'dashboard' },
        { label: 'Recall', href: appRoutes.recall, icon: 'recall' }
      ]
    },
    {
      label: 'Memory',
      links: [
        { label: 'Memory map', href: appRoutes.explore, icon: 'explore' },
        { label: 'Sources', href: appRoutes.sources, icon: 'sources' },
        { label: 'Findings', href: appRoutes.findings, icon: 'findings' },
        { label: 'Subjects', href: appRoutes.subjects, icon: 'subjects' },
        { label: 'Themes', href: appRoutes.themes, icon: 'themes' }
      ]
    },
    {
      label: 'Operations',
      links: [
        { label: 'Usage', href: appRoutes.usage, icon: 'usage' },
        { label: 'Processing', href: appRoutes.processing, icon: 'processing' }
      ]
    },
    {
      label: 'Collaboration',
      links: [{ label: 'Organizations', href: appRoutes.organizations, icon: 'organizations' }]
    }
  ];
}
