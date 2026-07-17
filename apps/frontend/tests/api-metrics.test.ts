import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { fetchMetrics, parseMetricsResponse } from '@/lib/api/metrics';
import type { MetricsResponse } from '@/lib/types/metrics';

const validPayload: MetricsResponse = {
  schema_version: '1.0',
  generated_at: '2026-07-17T12:00:00Z',
  history: {
    bootstrap_version: 1,
    baseline_at: '2026-07-17T10:00:00Z',
    complete_since: '2026-07-17T11:00:00Z',
    completeness: 'COMPLETE',
    baseline_event_count: 5,
  },
  capabilities: {
    job_offers_saved: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    job_offers_analyzed: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    resumes_generated: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    applications_created: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    applications_submitted: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    employer_responses: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    interviews: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    rejections: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    applications_accepted: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    status_changes: {
      current: 'UNSUPPORTED',
      lifetime: 'AVAILABLE',
      peak: 'UNSUPPORTED',
      archived: 'UNSUPPORTED',
    },
  },
  metrics: [
    {
      metric_name: 'job_offers_saved',
      current: { value: 2, availability: 'AVAILABLE' },
      lifetime: { value: 10, availability: 'AVAILABLE' },
      peak: { value: 5, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'job_offers_analyzed',
      current: { value: 1, availability: 'AVAILABLE' },
      lifetime: { value: 5, availability: 'AVAILABLE' },
      peak: { value: 3, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'resumes_generated',
      current: { value: 3, availability: 'AVAILABLE' },
      lifetime: { value: 8, availability: 'AVAILABLE' },
      peak: { value: 4, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'applications_created',
      current: { value: 4, availability: 'AVAILABLE' },
      lifetime: { value: 12, availability: 'AVAILABLE' },
      peak: { value: 6, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'applications_submitted',
      current: { value: 3, availability: 'AVAILABLE' },
      lifetime: { value: 9, availability: 'AVAILABLE' },
      peak: { value: 5, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'employer_responses',
      current: { value: 2, availability: 'AVAILABLE' },
      lifetime: { value: 6, availability: 'AVAILABLE' },
      peak: { value: 3, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'interviews',
      current: { value: 1, availability: 'AVAILABLE' },
      lifetime: { value: 4, availability: 'AVAILABLE' },
      peak: { value: 2, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'rejections',
      current: { value: 1, availability: 'AVAILABLE' },
      lifetime: { value: 3, availability: 'AVAILABLE' },
      peak: { value: 2, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'applications_accepted',
      current: { value: 1, availability: 'AVAILABLE' },
      lifetime: { value: 2, availability: 'AVAILABLE' },
      peak: { value: 1, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'status_changes',
      current: { value: null, availability: 'UNSUPPORTED' },
      lifetime: { value: 20, availability: 'AVAILABLE' },
      peak: { value: null, availability: 'UNSUPPORTED' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
  ],
};

describe('parseMetricsResponse validation logic', () => {
  it('parser accepts realistic COMPLETE payload', () => {
    const result = parseMetricsResponse(validPayload);
    expect(result.schema_version).toBe('1.0');
    expect(result.metrics).toHaveLength(10);
  });

  it('parser accepts realistic PARTIAL_BASELINE payload', () => {
    const partialPayload = {
      ...validPayload,
      history: {
        ...validPayload.history,
        completeness: 'PARTIAL_BASELINE' as const,
      },
    };
    const result = parseMetricsResponse(partialPayload);
    expect(result.history.completeness).toBe('PARTIAL_BASELINE');
  });

  it('bad schema_version is rejected', () => {
    const invalidSchema = { ...validPayload, schema_version: '2.0' };
    expect(() => parseMetricsResponse(invalidSchema)).toThrow('Invalid schema version');
  });

  it('missing required metric is rejected', () => {
    const missingMetric = {
      ...validPayload,
      metrics: validPayload.metrics.filter((m) => m.metric_name !== 'status_changes'),
    };
    expect(() => parseMetricsResponse(missingMetric)).toThrow(
      'Missing required metric: status_changes'
    );
  });

  it('missing required capability is rejected', () => {
    const badCaps = { ...validPayload.capabilities };
    delete (badCaps as Record<string, unknown>).status_changes;
    const missingCap = { ...validPayload, capabilities: badCaps };
    expect(() => parseMetricsResponse(missingCap)).toThrow(
      'Missing required capability for metric: status_changes'
    );
  });

  it('inconsistency capability and availability is rejected', () => {
    const inconsistent = {
      ...validPayload,
      metrics: validPayload.metrics.map((m) => {
        if (m.metric_name === 'status_changes') {
          return {
            ...m,
            current: { value: 1, availability: 'AVAILABLE' as const }, // capability is UNSUPPORTED
          };
        }
        return m;
      }),
    };
    expect(() => parseMetricsResponse(inconsistent)).toThrow(
      'Metric status_changes: current.availability does not match capability'
    );
  });

  it('duplicate metric_name is rejected', () => {
    const duplicate = {
      ...validPayload,
      metrics: [...validPayload.metrics, validPayload.metrics[0]],
    };
    expect(() => parseMetricsResponse(duplicate)).toThrow(
      'Duplicate metric name found: job_offers_saved'
    );
  });

  it('unknown metric does not crash', () => {
    const payloadWithUnknown = {
      ...validPayload,
      metrics: [
        ...validPayload.metrics,
        {
          metric_name: 'unknown_metric_dimension',
          current: { value: 1, availability: 'AVAILABLE' },
          lifetime: { value: 1, availability: 'AVAILABLE' },
          peak: { value: 1, availability: 'AVAILABLE' },
          archived: { value: 1, availability: 'AVAILABLE' },
        },
      ],
    };
    const result = parseMetricsResponse(payloadWithUnknown);
    expect(result.metrics).toHaveLength(10); // Unknown metric skipped without crash
  });

  it('NaN is rejected', () => {
    const nanVal = {
      ...validPayload,
      metrics: validPayload.metrics.map((m) => {
        if (m.metric_name === 'job_offers_saved') {
          return {
            ...m,
            current: { value: NaN, availability: 'AVAILABLE' as const },
          };
        }
        return m;
      }),
    };
    expect(() => parseMetricsResponse(nanVal)).toThrow(
      'Metric job_offers_saved: current.value must be a non-negative safe integer'
    );
  });

  it('Infinity is rejected', () => {
    const infVal = {
      ...validPayload,
      metrics: validPayload.metrics.map((m) => {
        if (m.metric_name === 'job_offers_saved') {
          return {
            ...m,
            current: { value: Infinity, availability: 'AVAILABLE' as const },
          };
        }
        return m;
      }),
    };
    expect(() => parseMetricsResponse(infVal)).toThrow(
      'Metric job_offers_saved: current.value must be a non-negative safe integer'
    );
  });

  it('fractional number is rejected', () => {
    const fracVal = {
      ...validPayload,
      metrics: validPayload.metrics.map((m) => {
        if (m.metric_name === 'job_offers_saved') {
          return {
            ...m,
            current: { value: 1.5, availability: 'AVAILABLE' as const },
          };
        }
        return m;
      }),
    };
    expect(() => parseMetricsResponse(fracVal)).toThrow(
      'Metric job_offers_saved: current.value must be a non-negative safe integer'
    );
  });

  it('negative number is rejected', () => {
    const negVal = {
      ...validPayload,
      metrics: validPayload.metrics.map((m) => {
        if (m.metric_name === 'job_offers_saved') {
          return {
            ...m,
            current: { value: -2, availability: 'AVAILABLE' as const },
          };
        }
        return m;
      }),
    };
    expect(() => parseMetricsResponse(negVal)).toThrow(
      'Metric job_offers_saved: current.value must be a non-negative safe integer'
    );
  });

  it('UNSUPPORTED with numerical value is rejected', () => {
    const unsupportedVal = {
      ...validPayload,
      metrics: validPayload.metrics.map((m) => {
        if (m.metric_name === 'status_changes') {
          return {
            ...m,
            current: { value: 5, availability: 'UNSUPPORTED' as const },
          };
        }
        return m;
      }),
    };
    expect(() => parseMetricsResponse(unsupportedVal)).toThrow(
      'Metric status_changes: current.value must be exactly null when UNSUPPORTED'
    );
  });

  it('UNSUPPORTED with undefined value is rejected', () => {
    const undefinedVal = {
      ...validPayload,
      metrics: validPayload.metrics.map((m) => {
        if (m.metric_name === 'status_changes') {
          return {
            ...m,
            current: { value: undefined, availability: 'UNSUPPORTED' as const },
          };
        }
        return m;
      }),
    };
    expect(() => parseMetricsResponse(undefinedVal)).toThrow(
      'Metric status_changes: current.value must be exactly null when UNSUPPORTED'
    );
  });
});

describe('fetchMetrics client API', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  it('requests GET /metrics and returns parsed data', async () => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify(validPayload), { status: 200 }));

    const data = await fetchMetrics();
    expect(data.schema_version).toBe('1.0');
    const [url, options] = fetchMock.mock.calls[0];
    expect(String(url)).toContain('/metrics');
    expect(options?.method).toBe(undefined); // default GET
  });

  it('throws error on HTTP failure status', async () => {
    fetchMock.mockResolvedValue(
      new Response(JSON.stringify({ detail: 'Forbidden access' }), { status: 403 })
    );

    await expect(fetchMetrics()).rejects.toThrow('Forbidden access');
  });
});
