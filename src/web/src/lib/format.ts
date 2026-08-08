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

export function formatAge(value: string | null | undefined): string {
  if (!value) return 'at an unknown time';
  const seconds = Math.max(0, Math.round((Date.now() - new Date(value).getTime()) / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours} hr ago`;
  return `${Math.round(hours / 24)} days ago`;
}
