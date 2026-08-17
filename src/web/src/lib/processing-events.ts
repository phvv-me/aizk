import type { ProcessingReport } from '$lib/api';

export type ProcessingConnection = 'connecting' | 'live' | 'reconnecting' | 'paused';

type ReportHandler = (report: ProcessingReport) => void;
type StatusHandler = (status: ProcessingConnection) => void;
type EventSourceFactory = (url: string) => EventSource;
type ReportLoader = () => Promise<ProcessingReport>;

const pollIntervalMilliseconds = 10_000;

async function loadProcessing(): Promise<ProcessingReport> {
  const response = await fetch('/app/processing/poll');
  if (!response.ok) throw new Error('processing updates are unavailable');
  return (await response.json()) as ProcessingReport;
}

/** Own one native processing event connection with browser-managed retry behavior. */
export class ProcessingEvents {
  private source?: EventSource;
  private polling?: ReturnType<typeof setInterval>;

  constructor(
    private readonly report: ReportHandler,
    private readonly status: StatusHandler,
    private readonly createSource: EventSourceFactory = (url) => new EventSource(url),
    private readonly loadReport: ReportLoader = loadProcessing
  ) {}

  start(): void {
    if (this.source || this.polling) return;
    this.status('connecting');
    const source = this.createSource('/events/processing');
    this.source = source;
    source.onopen = () => this.status('live');
    source.onerror = () => {
      source.close();
      this.source = undefined;
      this.startPolling();
    };
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
    if (this.polling) clearInterval(this.polling);
    this.polling = undefined;
    this.status(status);
  }

  private startPolling(): void {
    this.status('reconnecting');
    const refresh = async () => {
      try {
        this.report(await this.loadReport());
        this.status('live');
      } catch {
        this.status('reconnecting');
      }
    };
    void refresh();
    this.polling = setInterval(refresh, pollIntervalMilliseconds);
  }
}
