export type AppView =
  | 'overview'
  | 'find'
  | 'explore'
  | 'sources'
  | 'findings'
  | 'subjects'
  | 'themes'
  | 'usage'
  | 'processing'
  | 'organizations';

export type JsonValue =
  | string
  | number
  | boolean
  | null
  | JsonValue[]
  | { [key: string]: JsonValue };

export type OrganizationProfile = {
  name: string;
  description: string | null;
  roles: string[];
  permissions: string[];
  writable: boolean;
  public: boolean;
};

export type Me = {
  label: string | null;
  admin: boolean;
  organizations: OrganizationProfile[];
};

export type KnowledgeTotals = {
  documents: number;
  files: number;
  findings: number;
  subjects: number;
  themes: number;
};

export type RecentDocument = {
  source_uri: string;
  date: string;
  scopes: string[];
  title: string;
  kind: string;
};

export type ArtifactView = {
  source_uri: string;
  date: string;
  scopes: string[];
  name: string;
  status: string;
  detail: string;
};

export type Overview = {
  totals: KnowledgeTotals;
  usage: {
    finds: number;
    keeps: number;
    files: number;
    uploaded_bytes: number;
    downloaded_bytes: number;
  };
  recent_documents: RecentDocument[];
  artifacts: ArtifactView[];
};

export type StageEstimate = {
  key: string;
  queued: number;
  running: number | null;
  failed: number | null;
  completed_1h: number;
  completed_24h: number;
  progress_percent: number;
  throughput_per_hour: number;
  throughput_window_hours: number | null;
  lower_seconds: number | null;
  upper_seconds: number | null;
  confidence: string;
  eta_status: string;
  oldest_at: string | null;
};

export type ProcessingReport = {
  generated_at: string;
  state: 'idle' | 'active' | 'delayed';
  stages: StageEstimate[];
  findable_lower_seconds: number | null;
  findable_upper_seconds: number | null;
  enriched_lower_seconds: number | null;
  enriched_upper_seconds: number | null;
  recent: ArtifactView[];
};

export type UsagePoint = {
  bucket: string;
  operation: string;
  requests: number;
  items: number;
  duration_ms: number;
};

export type UsageSummary = {
  finds: number;
  keeps: number;
  files: number;
  shares: number;
  artifact_reads: number;
  requests: number;
  items: number;
  duration_ms: number;
};

export type UsageReport = {
  generated_at: string;
  recorded_through: string;
  days: number;
  start: string;
  summary: UsageSummary;
  lifetime: UsageSummary;
  points: UsagePoint[];
};

export type GraphNode = {
  id: string;
  label: string;
  degree: number;
};

export type GraphEdge = {
  source: string;
  target: string;
  predicate: string;
  statement: string;
};

export type GraphSlice = {
  nodes: GraphNode[];
  edges: GraphEdge[];
  truncated: boolean;
};

export type DashboardData = {
  me: Me;
  overview: Overview | null;
  processing: ProcessingReport | null;
  usage: UsageReport | null;
  graph: GraphSlice | null;
};
