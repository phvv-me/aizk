import type { ProcessingReport } from '$lib/api';

export type ProcessingConnection = 'connecting' | 'live' | 'reconnecting' | 'paused';

type ReportHandler = (report: ProcessingReport) => void;
type StatusHandler = (status: ProcessingConnection) => void;
type EventSourceFactory = (url: string) => EventSource;

/** Own one native processing event connection with browser-managed retry behavior. */
export class ProcessingEvents {
  private source?: EventSource;

  constructor(
    private readonly report: ReportHandler,
    private readonly status: StatusHandler,
    private readonly createSource: EventSourceFactory = (url) => new EventSource(url)
  ) {}

  start(): void {
    if (this.source) return;
    this.status('connecting');
    const source = this.createSource('/events/processing');
    this.source = source;
    source.onopen = () => this.status('live');
    source.onerror = () => this.status('reconnecting');
    source.addEventListener('processing', (event) => {
      try {
        this.report(JSON.parse((event as MessageEvent<string>).data) as ProcessingReport);
        this.status('live');
      } catch {
        this.status('reconnecting');
      }
    });
  }

  stop(status: ProcessingConnection = 'paused'): void {
    this.source?.close();
    this.source = undefined;
    this.status(status);
  }
}
