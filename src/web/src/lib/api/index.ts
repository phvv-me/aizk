/**
 * Typed browser API surface, re-exported from the client generated out of the
 * server's OpenAPI schema (`pnpm generate`) under the page-facing names.
 */
export type {
  Answer,
  ArtifactView,
  FindingPage,
  FindingView,
  GraphEdge,
  GraphNode,
  GraphSlice,
  KnowledgeTotals,
  Me,
  OrganizationDirectory,
  OrganizationMemberView as OrganizationMember,
  OrganizationView as OrganizationEntry,
  OrganizationProfile as Organization,
  Overview,
  ProcessingReport,
  RecentSource,
  SourcePage,
  SourceView,
  StageEstimate,
  SubjectPage,
  SubjectView,
  ThemePage,
  ThemeView,
  UsagePoint,
  UsageReport,
  UsageSummary,
  UsageTotals
} from './generated';

/** The organization roles a member can hold, least to most capable. */
export const memberRoles = ['viewer', 'editor', 'admin'] as const;

/** Format a byte count compactly for metric cards. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes;
  let unit = 'B';
  for (const next of units) {
    if (value < 1024) break;
    value /= 1024;
    unit = next;
  }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${unit}`;
}
