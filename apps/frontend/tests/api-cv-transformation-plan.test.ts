import { beforeEach, describe, expect, it, vi } from 'vitest';
import { apiFetch } from '@/lib/api/client';
import {
  CVTransformationPlanApiError,
  fetchCVTransformationPlan,
  parseCVTransformationPlan,
} from '@/lib/api/cv-transformation-plan';
import type { CVTransformationPlan } from '@/lib/types/cv-transformation-plan';

vi.mock('@/lib/api/client', () => ({ apiFetch: vi.fn() }));

const payload: CVTransformationPlan = {
  plan_version: '1.1',
  resume_id: 'resume-1',
  job_id: 'job-1',
  generated_at: '2026-07-18T12:00:00Z',
  detected_job_level: 'MANAGER',
  candidate_profile_level: 'EXPERT',
  positioning_strategy: 'CONTROLLED_ELEVATION',
  requires_human_review: true,
  summary_plan: [
    {
      action: 'HUMAN_REVIEW',
      section: 'SUMMARY',
      source_reference: 'resume:summary:0001',
      display_label: 'Professional summary',
      reason_code: 'CONTROLLED_ELEVATION_REVIEW',
      reason: 'Review required.',
      evidence_strength: 'WEAK',
      human_review_required: true,
    },
  ],
  experience_plan: [],
  competencies_plan: [],
  achievements_plan: [],
  tools_plan: [],
  evidence_permissions: [
    {
      claim_code: 'STRATEGY_RESPONSIBILITY_EVIDENCE',
      source_reference: 'resume:summary:0001',
      evidence_strength: 'STRONG',
      allowed_operation: 'REPHRASE_EXISTING',
      human_review_required: false,
    },
  ],
  prohibited_claims: [
    {
      code: 'BOARD_WITHOUT_EVIDENCE',
      reason_code: 'EVIDENCE_REQUIRED_FOR_NEW_CLAIM',
      reason: 'No approved evidence.',
      human_review_required: true,
    },
    {
      code: 'BUDGET_WITHOUT_EVIDENCE',
      reason_code: 'EVIDENCE_REQUIRED_FOR_NEW_CLAIM',
      reason: 'No approved evidence.',
      human_review_required: true,
    },
    {
      code: 'CERTIFICATION_WITHOUT_EVIDENCE',
      reason_code: 'EVIDENCE_REQUIRED_FOR_NEW_CLAIM',
      reason: 'No approved evidence.',
      human_review_required: true,
    },
    {
      code: 'LANGUAGE_LEVEL_WITHOUT_EVIDENCE',
      reason_code: 'EVIDENCE_REQUIRED_FOR_NEW_CLAIM',
      reason: 'No approved evidence.',
      human_review_required: true,
    },
    {
      code: 'MANAGING_MANAGERS_WITHOUT_EVIDENCE',
      reason_code: 'EVIDENCE_REQUIRED_FOR_NEW_CLAIM',
      reason: 'No approved evidence.',
      human_review_required: true,
    },
    {
      code: 'PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE',
      reason_code: 'EVIDENCE_REQUIRED_FOR_NEW_CLAIM',
      reason: 'No approved evidence.',
      human_review_required: true,
    },
    {
      code: 'PNL_WITHOUT_EVIDENCE',
      reason_code: 'EVIDENCE_REQUIRED_FOR_NEW_CLAIM',
      reason: 'No approved evidence.',
      human_review_required: true,
    },
    {
      code: 'QUANTIFIED_RESULT_WITHOUT_EVIDENCE',
      reason_code: 'EVIDENCE_REQUIRED_FOR_NEW_CLAIM',
      reason: 'No approved evidence.',
      human_review_required: true,
    },
    {
      code: 'TEAM_SIZE_WITHOUT_EVIDENCE',
      reason_code: 'EVIDENCE_REQUIRED_FOR_NEW_CLAIM',
      reason: 'No approved evidence.',
      human_review_required: true,
    },
    {
      code: 'TECHNICAL_SKILL_WITHOUT_EVIDENCE',
      reason_code: 'EVIDENCE_REQUIRED_FOR_NEW_CLAIM',
      reason: 'No approved evidence.',
      human_review_required: true,
    },
  ],
  limitations: [],
  source_summary: {
    approved_truth_items_count: 2,
    resume_items_count: 1,
    job_requirements_count: 3,
    career_positioning_schema_version: '1.0',
  },
};

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function copy(): Record<string, unknown> {
  return JSON.parse(JSON.stringify(payload)) as Record<string, unknown>;
}

beforeEach(() => vi.clearAllMocks());

describe('fetchCVTransformationPlan', () => {
  it('1. uses the public jobs/resumes transformation-plan URL', async () => {
    vi.mocked(apiFetch).mockResolvedValue(response(payload));
    await fetchCVTransformationPlan('job-1', 'resume-1');
    expect(apiFetch).toHaveBeenCalledWith(
      '/jobs/job-1/resumes/resume-1/transformation-plan',
      expect.objectContaining({ method: 'GET' })
    );
  });

  it('2. URL-encodes both identifiers', async () => {
    vi.mocked(apiFetch).mockResolvedValue(response(payload));
    await fetchCVTransformationPlan('job /?x', 'resume/#1');
    expect(vi.mocked(apiFetch).mock.calls[0][0]).toBe(
      '/jobs/job%20%2F%3Fx/resumes/resume%2F%231/transformation-plan'
    );
  });

  it('3. makes a GET request without a request body', async () => {
    vi.mocked(apiFetch).mockResolvedValue(response(payload));
    await fetchCVTransformationPlan('job-1', 'resume-1');
    expect(vi.mocked(apiFetch).mock.calls[0][1]).toEqual({ method: 'GET', signal: undefined });
  });

  it('4. forwards AbortSignal', async () => {
    vi.mocked(apiFetch).mockResolvedValue(response(payload));
    const controller = new AbortController();
    await fetchCVTransformationPlan('job-1', 'resume-1', controller.signal);
    expect(vi.mocked(apiFetch).mock.calls[0][1]?.signal).toBe(controller.signal);
  });

  it('5. parses the server response without recomputing it', async () => {
    vi.mocked(apiFetch).mockResolvedValue(response(payload));
    const result = await fetchCVTransformationPlan('job-1', 'resume-1');
    expect(result).toEqual(payload);
  });

  it('6. exposes a typed 404 error', async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      response({ detail: { code: 'RESUME_NOT_FOUND', message: 'Resume not found' } }, 404)
    );
    await expect(fetchCVTransformationPlan('job-1', 'missing')).rejects.toMatchObject({
      name: 'CVTransformationPlanApiError',
      code: 'RESUME_NOT_FOUND',
      status: 404,
    });
  });

  it('7. exposes a typed 422 error', async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      response({ detail: { code: 'INCONSISTENT_JOB_RESUME_RELATION', message: 'Mismatch' } }, 422)
    );
    await expect(fetchCVTransformationPlan('job-1', 'resume-1')).rejects.toMatchObject({
      code: 'INCONSISTENT_JOB_RESUME_RELATION',
      status: 422,
    });
  });

  it('8. converts a network error without a fallback request', async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error('offline'));
    await expect(fetchCVTransformationPlan('job-1', 'resume-1')).rejects.toEqual(
      expect.objectContaining({ code: 'NETWORK_OR_CONTRACT_ERROR', status: 0 })
    );
    expect(apiFetch).toHaveBeenCalledTimes(1);
  });

  it('9. preserves abort errors', async () => {
    const error = new Error('aborted');
    error.name = 'AbortError';
    vi.mocked(apiFetch).mockRejectedValue(error);
    await expect(fetchCVTransformationPlan('job-1', 'resume-1')).rejects.toBe(error);
  });
});

describe('parseCVTransformationPlan', () => {
  it('10. accepts the backend-compatible contract', () => {
    expect(parseCVTransformationPlan(payload)).toEqual(payload);
  });

  it('11. rejects private or unknown root fields', () => {
    const value = copy();
    value.metadata_json = { private: true };
    expect(() => parseCVTransformationPlan(value)).toThrow(
      'Unexpected field transformation plan.metadata_json.'
    );
  });

  it('12. rejects private fields inside plan items', () => {
    const value = copy();
    const items = value.summary_plan as Array<Record<string, unknown>>;
    items[0].truth_item_id = 'private-id';
    expect(() => parseCVTransformationPlan(value)).toThrow(
      'Unexpected field plan item.truth_item_id.'
    );
  });

  it('13. rejects an invalid safe source reference', () => {
    const value = copy();
    const items = value.summary_plan as Array<Record<string, unknown>>;
    items[0].source_reference = 'private-source-reference';
    expect(() => parseCVTransformationPlan(value)).toThrow('Invalid safe source_reference.');
  });

  it('14. rejects an invalid action instead of calculating a client fallback', () => {
    const value = copy();
    const items = value.summary_plan as Array<Record<string, unknown>>;
    items[0].action = 'GENERATE';
    expect(() => parseCVTransformationPlan(value)).toThrow('Invalid plan item enum value.');
  });

  it('15. exports the dedicated API error class', () => {
    const error = new CVTransformationPlanApiError('TEST', 418, 'test');
    expect(error).toMatchObject({
      name: 'CVTransformationPlanApiError',
      code: 'TEST',
      status: 418,
    });
  });

  it('16. parses item-scoped EvidencePermission', () => {
    const result = parseCVTransformationPlan(payload);
    expect(result.evidence_permissions).toEqual([
      {
        claim_code: 'STRATEGY_RESPONSIBILITY_EVIDENCE',
        source_reference: 'resume:summary:0001',
        evidence_strength: 'STRONG',
        allowed_operation: 'REPHRASE_EXISTING',
        human_review_required: false,
      },
    ]);
  });

  it('17. parses the short display_label', () => {
    expect(parseCVTransformationPlan(payload).summary_plan[0].display_label).toBe(
      'Professional summary'
    );
  });

  it('18. requires the complete stable set of ten guardrails', () => {
    const result = parseCVTransformationPlan(payload);
    expect(result.prohibited_claims).toHaveLength(10);
    expect(result.prohibited_claims.map((claim) => claim.code)).toEqual(
      [...result.prohibited_claims.map((claim) => claim.code)].sort()
    );
  });

  it('19. rejects duplicate prohibited claims', () => {
    const value = copy();
    const claims = value.prohibited_claims as Array<Record<string, unknown>>;
    claims[9].code = claims[0].code;
    expect(() => parseCVTransformationPlan(value)).toThrow(
      'prohibited_claims must contain all unique sorted guardrails.'
    );
  });

  it('20. rejects duplicate source_reference values', () => {
    const value = copy();
    const summary = (value.summary_plan as Array<Record<string, unknown>>)[0];
    value.summary_plan = [summary, { ...summary }];
    expect(() => parseCVTransformationPlan(value)).toThrow(
      'source_reference values must be unique.'
    );
  });

  it('21. rejects inconsistent HUMAN_REVIEW propagation', () => {
    const value = copy();
    value.requires_human_review = false;
    expect(() => parseCVTransformationPlan(value)).toThrow(
      'HUMAN_REVIEW must propagate to requires_human_review.'
    );
  });

  it('22. rejects HUMAN_REVIEW with a false item-level review flag', () => {
    const value = copy();
    const items = value.summary_plan as Array<Record<string, unknown>>;
    items[0].human_review_required = false;
    expect(() => parseCVTransformationPlan(value)).toThrow(
      'HUMAN_REVIEW action requires human_review_required=true.'
    );
  });

  it('23. rejects GENERATE_NEW as an allowed operation', () => {
    const value = copy();
    const permissions = value.evidence_permissions as Array<Record<string, unknown>>;
    permissions[0].allowed_operation = 'GENERATE_NEW';
    expect(() => parseCVTransformationPlan(value)).toThrow('Invalid evidence permission.');
  });

  it('24. rejects private fields in EvidencePermission', () => {
    const value = copy();
    const permissions = value.evidence_permissions as Array<Record<string, unknown>>;
    permissions[0].truth_item_id = 'private-id';
    expect(() => parseCVTransformationPlan(value)).toThrow(
      'Unexpected field evidence permission.truth_item_id.'
    );
  });

  it('25. rejects EvidencePermission for an unknown item', () => {
    const value = copy();
    const permissions = value.evidence_permissions as Array<Record<string, unknown>>;
    permissions[0].source_reference = 'resume:tools:9999';
    expect(() => parseCVTransformationPlan(value)).toThrow(
      'Evidence permission references an unknown plan item.'
    );
  });
});
