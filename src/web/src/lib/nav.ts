import { adminRoutes, appRoutes } from './routes';

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
  | 'organizations'
  | 'overview'
  | 'queues'
  | 'ingestion'
  | 'storage';

export type NavLink = {
  label: string;
  href: string;
  icon: NavIcon;
};

export type NavSection = {
  label: string;
  links: NavLink[];
};

/** Build the information architecture one caller sees, operator tools included when earned.

 * The console is a section of the application rather than a second application, so an
 * operator keeps one sidebar, one session and one origin. `operator` comes from the API,
 * which is the only party able to prove the role, and is false whenever it cannot be asked.
 */
export function navigation(operator: boolean = false): NavSection[] {
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
    },
    ...(operator ? adminNavigation() : [])
  ];
}

/** Build the fixed operator console information architecture. */
export function adminNavigation(): NavSection[] {
  return [
    {
      label: 'Operator',
      links: [
        { label: 'Overview', href: adminRoutes.overview, icon: 'overview' },
        { label: 'Queues', href: adminRoutes.queues, icon: 'queues' },
        { label: 'Ingestion', href: adminRoutes.ingestion, icon: 'ingestion' },
        { label: 'Storage', href: adminRoutes.storage, icon: 'storage' },
        { label: 'Usage', href: adminRoutes.usage, icon: 'usage' }
      ]
    }
  ];
}
