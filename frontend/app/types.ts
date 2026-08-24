export type ChartType = "metric" | "line" | "bar" | "donut" | "table";
export type TimeGranularity = "year" | "quarter" | "month" | "day";
export type AnswerMode = "rule" | "llm";
export type AnswerStatus = "pending" | "streaming" | "completed" | "failed";

export type DataColumn = {
  key: string;
  label: string;
  data_type: "string" | "number" | "date" | "boolean";
  unit: string;
  sensitive: boolean;
};

export type ChartSpec = {
  chart_type: ChartType;
  title: string;
  x_field: string | null;
  y_fields: string[];
  series_field: string | null;
  unit: string;
  sort: "asc" | "desc" | null;
  reason: string;
  available_time_drill: TimeGranularity[];
};

export type Insight = {
  headline: string;
  summary: string;
  evidence: string[];
  caveats: string[];
  confidence: "high" | "medium" | "low";
  inference: boolean;
};

export type QueryResponse = {
  api_version: string;
  query_id: string;
  session_id: string;
  status: "completed" | "failed";
  question: string;
  route: string;
  answer_mode: AnswerMode;
  answer_status: AnswerStatus;
  generated_at: string;
  duration_ms: number;
  plan: Record<string, unknown>;
  sql: string | null;
  columns: DataColumn[];
  records: Record<string, unknown>[];
  row_count: number;
  truncated: boolean;
  visualization: ChartSpec;
  alternatives: ChartSpec[];
  insight: Insight | null;
  current_time_level: TimeGranularity | null;
  available_time_drill: TimeGranularity[];
  drill_path: TimeGranularity[];
  aggregation: string | null;
  permissions: {
    can_view_sql: boolean;
    can_export: boolean;
    can_share: boolean;
    can_view_admin: boolean;
    can_query_data: boolean;
  };
  warnings: string[];
};

export type HistoryItem = {
  query_id: string;
  session_id: string;
  user_id: string;
  question: string;
  route: string;
  status: string;
  answer_mode: AnswerMode;
  answer_status: AnswerStatus;
  answer: string;
  row_count: number;
  created_at: string;
  duration_ms: number;
  thread_title: string | null;
};

export type AdminUserSummary = {
  user_id: string;
  operation_count: number;
  risk_event_count: number;
  last_active_at: string;
};

export type AuditItem = {
  event_id: string;
  query_id: string | null;
  user_id: string;
  action: string;
  risk_level: "low" | "medium" | "high";
  details: Record<string, unknown>;
  created_at: string;
};

export type SecurityAlert = {
  alert_id: string;
  source_event_id: string;
  rule_code: string;
  user_id: string;
  query_id: string | null;
  severity: "low" | "medium" | "high";
  status: "open" | "acknowledged" | "resolved";
  details: Record<string, unknown>;
  created_at: string;
  updated_at: string;
};
