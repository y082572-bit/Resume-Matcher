import { apiFetch } from './client';
import {
  MetricsResponse,
  MetricValue,
  MetricCapabilities,
  MetricMeasure,
  MetricType,
} from '../types/metrics';

const VALID_METRIC_TYPES: MetricType[] = [
  'job_offers_saved',
  'job_offers_analyzed',
  'resumes_generated',
  'applications_created',
  'applications_submitted',
  'employer_responses',
  'interviews',
  'rejections',
  'applications_accepted',
  'status_changes',
];

function isObject(val: unknown): val is Record<string, unknown> {
  return typeof val === 'object' && val !== null;
}

function isValidDate(val: string): boolean {
  const d = new Date(val);
  return !Number.isNaN(d.getTime()) && val.includes('Z'); // Strict UTC check
}

export function parseMetricsResponse(data: unknown): MetricsResponse {
  if (!isObject(data)) {
    throw new Error('Metrics response is not an object.');
  }

  if (data.schema_version !== '1.0') {
    throw new Error(`Invalid schema version: expected "1.0", got ${String(data.schema_version)}`);
  }

  if (typeof data.generated_at !== 'string' || !isValidDate(data.generated_at)) {
    throw new Error('Invalid generated_at timestamp.');
  }

  // Parse history info
  const history = data.history;
  if (!isObject(history)) {
    throw new Error('History information is missing or invalid.');
  }

  if (
    typeof history.bootstrap_version !== 'number' ||
    !Number.isSafeInteger(history.bootstrap_version) ||
    history.bootstrap_version < 0
  ) {
    throw new Error('Invalid bootstrap_version.');
  }

  if (typeof history.baseline_at !== 'string' || !isValidDate(history.baseline_at)) {
    throw new Error('Invalid baseline_at timestamp.');
  }

  if (typeof history.complete_since !== 'string' || !isValidDate(history.complete_since)) {
    throw new Error('Invalid complete_since timestamp.');
  }

  if (history.completeness !== 'COMPLETE' && history.completeness !== 'PARTIAL_BASELINE') {
    throw new Error(`Invalid completeness value: ${String(history.completeness)}`);
  }

  if (
    typeof history.baseline_event_count !== 'number' ||
    !Number.isSafeInteger(history.baseline_event_count) ||
    history.baseline_event_count < 0
  ) {
    throw new Error('Invalid baseline_event_count.');
  }

  // Parse capabilities
  const capabilities = data.capabilities;
  if (!isObject(capabilities)) {
    throw new Error('Capabilities are missing or invalid.');
  }

  const parsedCapabilities = {} as Record<MetricType, MetricCapabilities>;
  for (const type of VALID_METRIC_TYPES) {
    const cap = capabilities[type];
    if (!cap || !isObject(cap)) {
      throw new Error(`Missing required capability for metric: ${type}`);
    }

    const availabilities = ['AVAILABLE', 'UNSUPPORTED'];
    if (
      !availabilities.includes(String(cap.current)) ||
      !availabilities.includes(String(cap.lifetime)) ||
      !availabilities.includes(String(cap.peak)) ||
      !availabilities.includes(String(cap.archived))
    ) {
      throw new Error(`Invalid capability dimensions for ${type}`);
    }

    parsedCapabilities[type] = {
      current: cap.current as 'AVAILABLE' | 'UNSUPPORTED',
      lifetime: cap.lifetime as 'AVAILABLE' | 'UNSUPPORTED',
      peak: cap.peak as 'AVAILABLE' | 'UNSUPPORTED',
      archived: cap.archived as 'AVAILABLE' | 'UNSUPPORTED',
    };
  }

  // Parse metrics list
  const metrics = data.metrics;
  if (!Array.isArray(metrics)) {
    throw new Error('Metrics list is missing or not an array.');
  }

  const parsedMetrics: MetricValue[] = [];
  const seenMetricNames = new Set<MetricType>();

  for (const m of metrics) {
    if (!isObject(m)) {
      throw new Error('Metric item is not an object.');
    }

    const metricName = m.metric_name as MetricType;
    // Ignore unknown metric types so they do not crash the UI
    if (!VALID_METRIC_TYPES.includes(metricName)) {
      continue;
    }

    if (seenMetricNames.has(metricName)) {
      throw new Error(`Duplicate metric name found: ${metricName}`);
    }
    seenMetricNames.add(metricName);

    const cap = parsedCapabilities[metricName];

    const parseMeasure = (
      name: string,
      measure: unknown,
      expectedAvailability: 'AVAILABLE' | 'UNSUPPORTED'
    ): MetricMeasure => {
      if (!isObject(measure)) {
        throw new Error(`Metric ${String(metricName)}: ${name} is not an object.`);
      }
      if (measure.availability !== expectedAvailability) {
        throw new Error(
          `Metric ${String(metricName)}: ${name}.availability does not match capability.`
        );
      }
      const val = measure.value;
      if (expectedAvailability === 'AVAILABLE') {
        if (typeof val !== 'number' || !Number.isSafeInteger(val) || val < 0) {
          throw new Error(
            `Metric ${String(metricName)}: ${name}.value must be a non-negative safe integer.`
          );
        }
        return {
          value: val,
          availability: 'AVAILABLE',
        };
      } else {
        if (val !== null) {
          throw new Error(
            `Metric ${String(metricName)}: ${name}.value must be exactly null when UNSUPPORTED.`
          );
        }
        return {
          value: null,
          availability: 'UNSUPPORTED',
        };
      }
    };

    parsedMetrics.push({
      metric_name: metricName,
      current: parseMeasure('current', m.current, cap.current),
      lifetime: parseMeasure('lifetime', m.lifetime, cap.lifetime),
      peak: parseMeasure('peak', m.peak, cap.peak),
      archived: parseMeasure('archived', m.archived, cap.archived),
    });
  }

  // Fail controlled if any of the required metric types are missing from the list
  for (const type of VALID_METRIC_TYPES) {
    if (!parsedMetrics.some((m) => m.metric_name === type)) {
      throw new Error(`Missing required metric: ${type}`);
    }
  }

  return {
    schema_version: '1.0',
    generated_at: data.generated_at,
    history: {
      bootstrap_version: history.bootstrap_version,
      baseline_at: history.baseline_at,
      complete_since: history.complete_since,
      completeness: history.completeness as 'COMPLETE' | 'PARTIAL_BASELINE',
      baseline_event_count: history.baseline_event_count,
    },
    capabilities: parsedCapabilities,
    metrics: parsedMetrics,
  };
}

export async function fetchMetrics(): Promise<MetricsResponse> {
  const res = await apiFetch('/metrics', { credentials: 'include' });
  if (!res.ok) {
    const text = await res.text().catch(() => '');
    let detail = '';
    try {
      const data = JSON.parse(text);
      detail = data.detail || '';
    } catch {
      // ignore
    }
    throw new Error(detail || `Failed to fetch metrics (status ${res.status}).`);
  }
  const payload = await res.json();
  return parseMetricsResponse(payload);
}
