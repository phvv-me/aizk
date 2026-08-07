import { describe, expect, it } from 'vitest';
import { formatDuration, formatEtaRange, formatOperation, sentence } from '../src/lib/format';

describe('processing formatters', () => {
  it('formats bounded ETA ranges without false precision', () => {
    expect(formatEtaRange(600, 1200)).toBe('10 min to 20 min');
    expect(formatEtaRange(3600, 3600)).toBe('1 hr');
    expect(formatEtaRange(0, 0)).toBe('Complete');
    expect(formatEtaRange(null, 60)).toBe('ETA unavailable until more recent work completes');
  });

  it('formats durations and machine names for product copy', () => {
    expect(formatDuration(20)).toBe('under a minute');
    expect(formatDuration(3720)).toBe('1 hr 2 min');
    expect(sentence('graph_projection')).toBe('Graph projection');
  });
});

describe('formatOperation', () => {
  it('labels the stored recall operation without implying a second tool exists', () => {
    expect(formatOperation('recall')).toBe('Recall');
  });

  it('labels every other stored operation kind', () => {
    expect(formatOperation('remember_text')).toBe('Remember (text)');
    expect(formatOperation('remember_file')).toBe('Remember (file)');
    expect(formatOperation('share')).toBe('Share');
    expect(formatOperation('artifact_read')).toBe('Artifact read');
    expect(formatOperation('web_search')).toBe('Web search');
    expect(formatOperation('web_fetch')).toBe('Web fetch');
  });

  it('falls back to a sentence-cased label for an unmapped kind', () => {
    expect(formatOperation('unknown_kind')).toBe('Unknown kind');
  });
});
