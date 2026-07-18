/**
 * Typed browser API surface, re-exported from the client generated out of the
 * server's OpenAPI schema (`pnpm generate`) under the page-facing names.
 */
import type { ArtifactReceipt, WriteResult } from './generated';

export type {
  Answer,
  ArtifactView,
  KnowledgeTotals,
  Me,
  OrganizationDirectory,
  OrganizationMemberView as OrganizationMember,
  OrganizationView as OrganizationEntry,
  OrganizationProfile as Organization,
  Overview,
  RecentSource,
  UploadGrant,
  UsageTotals
} from './generated';

/** Receipt for one accepted write, either a direct document or a queued artifact. */
export type WriteReceipt = WriteResult | ArtifactReceipt;

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
