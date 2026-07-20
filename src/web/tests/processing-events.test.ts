import { describe, expect, it, vi } from 'vitest';
import type { ProcessingReport } from '../src/lib/api';
import { ProcessingEvents, type ProcessingConnection } from '../src/lib/processing-events';

class FakeEventSource {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  private processing?: (event: MessageEvent<string>) => void;

  addEventListener(name: string, listener: EventListener): void {
    if (name === 'processing') {
      this.processing = listener as (event: MessageEvent<string>) => void;
    }
  }

  emit(data: string): void {
    this.processing?.({ data } as MessageEvent<string>);
  }

  close(): void {
    this.closed = true;
  }
}

const report: ProcessingReport = {
  generated_at: '2026-07-20T00:00:00Z',
  state: 'idle',
  stages: [],
  recallable_lower_seconds: null,
  recallable_upper_seconds: null,
  enriched_lower_seconds: null,
  enriched_upper_seconds: null,
  recent: []
};

describe('ProcessingEvents', () => {
  it('opens once, delivers snapshots, reports reconnects, and closes cleanly', () => {
    const source = new FakeEventSource();
    const reports: ProcessingReport[] = [];
    const statuses: ProcessingConnection[] = [];
    const create = vi.fn(() => source as unknown as EventSource);
    const events = new ProcessingEvents(
      (next) => reports.push(next),
      (status) => statuses.push(status),
      create
    );

    events.start();
    events.start();
    source.onopen?.();
    source.emit(JSON.stringify(report));
    source.onerror?.();
    events.stop();

    expect(create).toHaveBeenCalledOnce();
    expect(reports).toEqual([report]);
    expect(statuses).toEqual(['connecting', 'live', 'live', 'reconnecting', 'paused']);
    expect(source.closed).toBe(true);
  });

  it('keeps the connection recoverable when one event is malformed', () => {
    const source = new FakeEventSource();
    const statuses: ProcessingConnection[] = [];
    const events = new ProcessingEvents(
      vi.fn(),
      (status) => statuses.push(status),
      () => source as unknown as EventSource
    );

    events.start();
    source.emit('{');
    events.stop('reconnecting');

    expect(statuses).toEqual(['connecting', 'reconnecting', 'reconnecting']);
  });
});
