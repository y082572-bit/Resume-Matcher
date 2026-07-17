import { describe, expect, it, vi, beforeEach } from 'vitest';
import {
  fetchCareerPositioning,
  fetchResumeJobContext,
  fetchResumePositioningContext,
  parseCareerPositioningResponse,
} from '@/lib/api/career-positioning';
import { CareerPositioningResponse } from '@/lib/types/career-positioning';
import { apiFetch } from '@/lib/api/client';

vi.mock('@/lib/api/client', () => ({
  apiFetch: vi.fn(),
  API_URL: 'http://test/api/v1',
  API_BASE: 'http://test',
}));

const validPayload: CareerPositioningResponse = {
  schema_version: '1.0',
  generated_at: '2026-07-17T12:00:00Z',
  job_id: 'job-123',
  job_title: 'Regional Manager',
  positioning: {
    detected_job_level: 'MANAGER',
    candidate_profile_level: 'MANAGER',
    level_gap: 0,
    positioning_strategy: 'MANAGER_LEADERSHIP',
    title_treatment: 'KEEP',
    fit_level: 'STRONG',
    fit_score: 95.0,
    confidence: 0.95,
    requires_human_review: false,
  },
  risks: {
    overqualification: { availability: 'AVAILABLE', level: 'NONE', rationale: ['OK'] },
    underqualification: { availability: 'AVAILABLE', level: 'NONE', rationale: ['OK'] },
    stability_concern: { availability: 'UNSUPPORTED', level: null, rationale: [] },
    sector_gap: { availability: 'AVAILABLE', level: 'NONE', rationale: ['OK'] },
    functional_gap: { availability: 'AVAILABLE', level: 'NONE', rationale: ['OK'] },
  },
  competencies: {
    direct_matches: [
      { name: 'team leading', reason: 'Direct keyword match.', source: 'Previous Job' },
    ],
    transferable_matches: [{ name: 'budgeting', reason: 'Mapped skill.', source: 'Previous Job' }],
    missing_critical: [],
    excessive_for_role: [],
  },
  strategy: {
    recommendation: 'Excellent match.',
    recommended_narrative: 'MANAGER',
    elements_to_emphasize: ['leadership', 'KPIs'],
    elements_to_flatten: [],
    elements_to_remove_or_deprioritize: [],
    title_positioning: 'Keep current title.',
    summary_positioning: 'Emphasize management.',
    narrative_variants: [
      {
        type: 'EXPERT',
        availability: 'AVAILABLE',
        recommended: false,
        title_positioning: 'Title Exp',
        summary_positioning: 'Summary Exp',
        elements_to_emphasize: ['skills'],
        elements_to_flatten: ['management'],
        elements_to_remove_or_deprioritize: [],
        risk_level: 'MEDIUM',
        limitations: [],
      },
      {
        type: 'MANAGER',
        availability: 'AVAILABLE',
        recommended: true,
        title_positioning: 'Title Mgr',
        summary_positioning: 'Summary Mgr',
        elements_to_emphasize: ['leadership', 'KPIs'],
        elements_to_flatten: [],
        elements_to_remove_or_deprioritize: [],
        risk_level: 'NONE',
        limitations: [],
      },
      {
        type: 'DIRECTOR',
        availability: 'UNSUPPORTED',
        recommended: false,
        title_positioning: null,
        summary_positioning: null,
        elements_to_emphasize: [],
        elements_to_flatten: [],
        elements_to_remove_or_deprioritize: [],
        risk_level: 'HIGH',
        limitations: ['No director scale'],
      },
    ],
  },
  evidence: {
    used_truth_items_count: 5,
    direct_evidence_count: 1,
    transferable_evidence_count: 1,
    unresolved_items_count: 0,
  },
  limitations: {
    insufficient_data: false,
    messages: [],
  },
};

function getPayloadCopy(): Record<string, unknown> {
  return JSON.parse(JSON.stringify(validPayload)) as Record<string, unknown>;
}

describe('parseCareerPositioningResponse', () => {
  it('1. parses valid payload successfully', () => {
    const res = parseCareerPositioningResponse(validPayload);
    expect(res.schema_version).toBe('1.0');
    expect(res.positioning.fit_score).toBe(95.0);
  });

  it('2. throws error on non-object payload', () => {
    expect(() => parseCareerPositioningResponse(null)).toThrow(
      'Career positioning response is not an object.'
    );
  });

  it('3. throws error on missing schema_version', () => {
    const copy = getPayloadCopy();
    delete copy.schema_version;
    expect(() => parseCareerPositioningResponse(copy)).toThrow('Invalid schema version');
  });

  it('4. throws error on incorrect schema_version', () => {
    const copy = getPayloadCopy();
    copy.schema_version = '2.0';
    expect(() => parseCareerPositioningResponse(copy)).toThrow('expected "1.0", got 2.0');
  });

  it('5. throws error on missing generated_at', () => {
    const copy = getPayloadCopy();
    delete copy.generated_at;
    expect(() => parseCareerPositioningResponse(copy)).toThrow('must be a non-empty string.');
  });

  it('6. throws error on invalid generated_at date format', () => {
    const copy = getPayloadCopy();
    copy.generated_at = 'not-a-date';
    expect(() => parseCareerPositioningResponse(copy)).toThrow(
      'generated_at timestamp must have a timezone'
    );
  });

  it('6b. throws error on invalid generated_at date format with timezone', () => {
    const copy = getPayloadCopy();
    copy.generated_at = '2026-13-45T12:00:00Z';
    expect(() => parseCareerPositioningResponse(copy)).toThrow('Invalid generated_at timestamp.');
  });

  it('7. throws error on missing job_id', () => {
    const copy = getPayloadCopy();
    delete copy.job_id;
    expect(() => parseCareerPositioningResponse(copy)).toThrow('must be a non-empty string.');
  });

  it('8. throws error on missing positioning', () => {
    const copy = getPayloadCopy();
    delete copy.positioning;
    expect(() => parseCareerPositioningResponse(copy)).toThrow(
      'Positioning details missing or invalid.'
    );
  });

  it('9. throws error on missing detected_job_level', () => {
    const copy = getPayloadCopy();
    const pos = copy.positioning as Record<string, unknown>;
    delete pos.detected_job_level;
    expect(() => parseCareerPositioningResponse(copy)).toThrow('must be a non-empty string.');
  });

  it('10. throws error on invalid detected_job_level enum', () => {
    const copy = getPayloadCopy();
    const pos = copy.positioning as Record<string, unknown>;
    pos.detected_job_level = 'JUNIOR_EXPERT';
    expect(() => parseCareerPositioningResponse(copy)).toThrow(
      'Invalid detected_job_level: JUNIOR_EXPERT'
    );
  });

  it('11. throws error on missing fit_level', () => {
    const copy = getPayloadCopy();
    const pos = copy.positioning as Record<string, unknown>;
    delete pos.fit_level;
    expect(() => parseCareerPositioningResponse(copy)).toThrow('must be a non-empty string.');
  });

  it('12. throws error on invalid fit_level enum', () => {
    const copy = getPayloadCopy();
    const pos = copy.positioning as Record<string, unknown>;
    pos.fit_level = 'EXTREME';
    expect(() => parseCareerPositioningResponse(copy)).toThrow('Invalid fit_level: EXTREME');
  });

  it('13. throws error on missing fit_score', () => {
    const copy = getPayloadCopy();
    const pos = copy.positioning as Record<string, unknown>;
    delete pos.fit_score;
    expect(() => parseCareerPositioningResponse(copy)).toThrow('is not a finite number.');
  });

  it('14. throws error on negative fit_score', () => {
    const copy = getPayloadCopy();
    const pos = copy.positioning as Record<string, unknown>;
    pos.fit_score = -5;
    expect(() => parseCareerPositioningResponse(copy)).toThrow('fit_score out of range');
  });

  it('15. throws error on fit_score > 100', () => {
    const copy = getPayloadCopy();
    const pos = copy.positioning as Record<string, unknown>;
    pos.fit_score = 101;
    expect(() => parseCareerPositioningResponse(copy)).toThrow('fit_score out of range');
  });

  it('16. throws error on non-finite fit_score', () => {
    const copy = getPayloadCopy();
    const pos = copy.positioning as Record<string, unknown>;
    pos.fit_score = NaN;
    expect(() => parseCareerPositioningResponse(copy)).toThrow('is not a finite number.');
  });

  it('17. throws error on missing risks', () => {
    const copy = getPayloadCopy();
    delete copy.risks;
    expect(() => parseCareerPositioningResponse(copy)).toThrow('Risks missing or invalid.');
  });

  it('18. throws error on missing risks fields', () => {
    const copy = getPayloadCopy();
    const r = copy.risks as Record<string, unknown>;
    delete r.overqualification;
    expect(() => parseCareerPositioningResponse(copy)).toThrow(
      'Risk item overqualification must be an object.'
    );
  });

  it('19. throws error on invalid risk availability', () => {
    const copy = getPayloadCopy();
    const r = copy.risks as Record<string, unknown>;
    const oq = r.overqualification as Record<string, unknown>;
    oq.availability = 'SOMETIMES';
    expect(() => parseCareerPositioningResponse(copy)).toThrow(
      'Invalid availability in overqualification: SOMETIMES'
    );
  });

  it('20. throws error on non-null risk level when unavailable', () => {
    const copy = getPayloadCopy();
    const r = copy.risks as Record<string, unknown>;
    const sc = r.stability_concern as Record<string, unknown>;
    sc.level = 'NONE';
    expect(() => parseCareerPositioningResponse(copy)).toThrow(
      'Risk level in stability_concern must be null when unavailable.'
    );
  });

  it('21. throws error on missing competencies', () => {
    const copy = getPayloadCopy();
    delete copy.competencies;
    expect(() => parseCareerPositioningResponse(copy)).toThrow('Competencies missing or invalid.');
  });

  it('22. throws error on non-array competencies field', () => {
    const copy = getPayloadCopy();
    const c = copy.competencies as Record<string, unknown>;
    c.direct_matches = 'not-an-array';
    expect(() => parseCareerPositioningResponse(copy)).toThrow(
      'Competencies: direct_matches is not an array.'
    );
  });

  it('23. throws error on non-object competency item', () => {
    const copy = getPayloadCopy();
    const c = copy.competencies as Record<string, unknown>;
    c.direct_matches = ['not-an-object'];
    expect(() => parseCareerPositioningResponse(copy)).toThrow(
      'Competency item in direct_matches is not an object.'
    );
  });

  it('24. throws error on competency item missing name', () => {
    const copy = getPayloadCopy();
    const c = copy.competencies as Record<string, unknown>;
    const dm = c.direct_matches as unknown[];
    const dm0 = dm[0] as Record<string, unknown>;
    delete dm0.name;
    expect(() => parseCareerPositioningResponse(copy)).toThrow('must be a non-empty string.');
  });

  it('25. throws error on duplicate competency name across lists', () => {
    const copy = getPayloadCopy();
    const c = copy.competencies as Record<string, unknown>;
    const tm = c.transferable_matches as unknown[];
    tm.push({
      name: 'team leading',
      reason: 'Another reason',
      source: 'Other Source',
    });
    expect(() => parseCareerPositioningResponse(copy)).toThrow(
      'Duplicate competency name found across lists: team leading'
    );
  });

  it('26. throws error on missing strategy', () => {
    const copy = getPayloadCopy();
    delete copy.strategy;
    expect(() => parseCareerPositioningResponse(copy)).toThrow('Strategy missing or invalid.');
  });

  it('27. throws error on invalid recommended_narrative', () => {
    const copy = getPayloadCopy();
    const s = copy.strategy as Record<string, unknown>;
    s.recommended_narrative = 'JUNIOR';
    expect(() => parseCareerPositioningResponse(copy)).toThrow(
      'Invalid recommended_narrative: JUNIOR'
    );
  });

  it('28. throws error on invalid strategy list types', () => {
    const copy = getPayloadCopy();
    const s = copy.strategy as Record<string, unknown>;
    s.elements_to_emphasize = 'not-an-array';
    expect(() => parseCareerPositioningResponse(copy)).toThrow('is not an array.');
  });

  it('29. throws error on duplicate narrative variant type', () => {
    const copy = getPayloadCopy();
    const s = copy.strategy as Record<string, unknown>;
    const nv = s.narrative_variants as unknown[];
    const nv1 = nv[1] as Record<string, unknown>;
    nv1.type = 'EXPERT';
    expect(() => parseCareerPositioningResponse(copy)).toThrow(
      'Duplicate narrative variant type: EXPERT'
    );
  });

  it('30. throws error on missing evidence', () => {
    const copy = getPayloadCopy();
    delete copy.evidence;
    expect(() => parseCareerPositioningResponse(copy)).toThrow('Evidence missing or invalid.');
  });

  it('31. throws error on invalid evidence negative integers', () => {
    const copy = getPayloadCopy();
    const e = copy.evidence as Record<string, unknown>;
    e.used_truth_items_count = -1;
    expect(() => parseCareerPositioningResponse(copy)).toThrow(
      'is not a non-negative safe integer.'
    );
  });

  it('32. throws error on missing limitations', () => {
    const copy = getPayloadCopy();
    delete copy.limitations;
    expect(() => parseCareerPositioningResponse(copy)).toThrow('Limitations missing or invalid.');
  });

  it('33. throws error on non-array limitations messages', () => {
    const copy = getPayloadCopy();
    const l = copy.limitations as Record<string, unknown>;
    l.messages = 'not-an-array';
    expect(() => parseCareerPositioningResponse(copy)).toThrow('is not an array.');
  });
});

describe('fetchCareerPositioning & fetchResumeJobContext & fetchResumePositioningContext', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('34. calls apiFetch with correct endpoint and parses response', async () => {
    const mockResponse = new Response(JSON.stringify(validPayload), {
      status: 200,
      headers: {
        'Content-Type': 'application/json',
      },
    });
    const mockFetch = vi.mocked(apiFetch).mockResolvedValue(mockResponse);
    const res = await fetchCareerPositioning('job-123');
    expect(mockFetch).toHaveBeenCalledWith('/jobs/job-123/career-positioning', {
      signal: undefined,
    });
    expect(res.job_title).toBe('Regional Manager');
  });

  it('35. fetchResumeJobContext fetches correct resume endpoint', async () => {
    const mockResponse = new Response(JSON.stringify({ job_id: 'job-999' }), {
      status: 200,
      headers: {
        'Content-Type': 'application/json',
      },
    });
    const mockFetch = vi.mocked(apiFetch).mockResolvedValue(mockResponse);
    const res = await fetchResumeJobContext('resume-123');
    expect(mockFetch).toHaveBeenCalledWith('/resumes/resume-123/job-description', {
      signal: undefined,
    });
    expect(res.job_id).toBe('job-999');
  });

  it('36. fetchResumePositioningContext fetches correct resume fields and validates', async () => {
    const mockResponse = new Response(
      JSON.stringify({ id: 'resume-123', parent_id: 'parent-123', content: 'extra' }),
      {
        status: 200,
        headers: {
          'Content-Type': 'application/json',
        },
      }
    );
    const mockFetch = vi.mocked(apiFetch).mockResolvedValue(mockResponse);
    const res = await fetchResumePositioningContext('resume-123');
    expect(mockFetch).toHaveBeenCalledWith('/resumes/resume-123', { signal: undefined });
    expect(res.resume_id).toBe('resume-123');
    expect(res.parent_id).toBe('parent-123');
    expect('content' in res).toBe(false);
  });
});
