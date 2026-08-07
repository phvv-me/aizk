export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined) return 'Not enough recent history';
  if (seconds <= 0) return 'Complete';
  if (seconds < 60) return 'under a minute';
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `${minutes} min`;
  const hours = Math.floor(minutes / 60);
  const remainder = minutes % 60;
  return remainder === 0 ? `${hours} hr` : `${hours} hr ${remainder} min`;
}

export function formatEtaRange(
  lower: number | null | undefined,
  upper: number | null | undefined
): string {
  if (lower === null || lower === undefined || upper === null || upper === undefined) {
    return 'ETA unavailable until more recent work completes';
  }
  if (upper <= 0) return 'Complete';
  const low = formatDuration(lower);
  const high = formatDuration(upper);
  return low === high ? low : `${low} to ${high}`;
}

export function formatDateTime(value: string | null | undefined): string {
  if (!value) return 'Not available';
  return new Intl.DateTimeFormat('en-US', {
    dateStyle: 'medium',
    timeStyle: 'short'
  }).format(new Date(value));
}

export function sentence(value: string): string {
  return value.replaceAll('_', ' ').replace(/^./, (letter) => letter.toUpperCase());
}

// The stored operation kind predates the `find` tool's rename and stays `recall` because
// every usage report column keys on it. This map keeps that one raw value out of the
// operator's eyes, the same way the caller-facing usage page already never prints it.
const OPERATION_LABELS: Record<string, string> = {
  recall: 'Recall',
  remember_text: 'Remember (text)',
  remember_file: 'Remember (file)',
  share: 'Share',
  artifact_read: 'Artifact read',
  web_search: 'Web search',
  web_fetch: 'Web fetch'
};

export function formatOperation(operation: string): string {
  return OPERATION_LABELS[operation] ?? sentence(operation);
}
