/**
 * Metrics TypeScript Definitions
 *
 * Must align exactly with apps/backend/app/schemas/metrics.py.
 */

export interface MetricMeasure {
  value: number | null;
  availability: 'AVAILABLE' | 'UNSUPPORTED';
}

export type MetricType =
  | 'job_offers_saved'
  | 'job_offers_analyzed'
  | 'resumes_generated'
  | 'applications_created'
  | 'applications_submitted'
  | 'employer_responses'
  | 'interviews'
  | 'rejections'
  | 'applications_accepted'
  | 'status_changes';

export interface MetricValue {
  metric_name: MetricType;
  current: MetricMeasure;
  lifetime: MetricMeasure;
  peak: MetricMeasure;
  archived: MetricMeasure;
}

export interface HistoryInfo {
  bootstrap_version: number;
  baseline_at: string; // ISO datetime string
  complete_since: string; // ISO datetime string
  completeness: 'COMPLETE' | 'PARTIAL_BASELINE';
  baseline_event_count: number;
}

export interface MetricCapabilities {
  current: 'AVAILABLE' | 'UNSUPPORTED';
  lifetime: 'AVAILABLE' | 'UNSUPPORTED';
  peak: 'AVAILABLE' | 'UNSUPPORTED';
  archived: 'AVAILABLE' | 'UNSUPPORTED';
}

export interface MetricsResponse {
  schema_version: '1.0';
  generated_at: string; // ISO datetime string
  history: HistoryInfo;
  capabilities: Record<MetricType, MetricCapabilities>;
  metrics: MetricValue[];
}
