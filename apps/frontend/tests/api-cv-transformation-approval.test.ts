import { beforeEach, describe, expect, it, vi } from 'vitest';

import { apiFetch } from '@/lib/api/client';
import {
  CVTransformationPlanApprovalApiError,
  fetchCVTransformationPlanApproval,
  parseCVTransformationPlanApproval,
  saveCVTransformationPlanApproval,
} from '@/lib/api/cv-transformation-approval';
import type { CVTransformationPlanApproval } from '@/lib/types/cv-transformation-approval';

vi.mock('@/lib/api/client', () => ({ apiFetch: vi.fn() }));

const approval: CVTransformationPlanApproval = {
  approval_id: 'approval-example',
  plan_version: '1.1',
  plan_fingerprint: 'a'.repeat(64),
  resume_id: 'resume-example',
  job_id: 'job-example',
  status: 'APPROVED',
  decisions: [{ source_reference: 'resume:summary:0001', decision: 'ACCEPT' }],
  guardrails_acknowledged: true,
  created_at: '2026-07-19T10:00:00Z',
  updated_at: '2026-07-19T10:00:00Z',
};

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function copy(): Record<string, unknown> {
  return JSON.parse(JSON.stringify(approval)) as Record<string, unknown>;
}

beforeEach(() => vi.clearAllMocks());

describe('CV transformation plan approval client', () => {
  it('1. parses the exact public approval contract', () => {
    expect(parseCVTransformationPlanApproval(approval)).toEqual(approval);
  });

  it('2. GET uses the public approval endpoint', async () => {
    vi.mocked(apiFetch).mockResolvedValue(response(approval));
    await fetchCVTransformationPlanApproval('job-example', 'resume-example');
    expect(apiFetch).toHaveBeenCalledWith(
      '/jobs/job-example/resumes/resume-example/transformation-plan/approval',
      { method: 'GET', signal: undefined }
    );
  });

  it('3. POST sends only backend fingerprint and explicit decisions', async () => {
    vi.mocked(apiFetch).mockResolvedValue(response(approval));
    const request = {
      plan_fingerprint: 'a'.repeat(64),
      decisions: [{ source_reference: 'resume:summary:0001', decision: 'ACCEPT' as const }],
      guardrails_acknowledged: true,
      submit: true,
    };
    await saveCVTransformationPlanApproval('job-example', 'resume-example', request);
    expect(apiFetch).toHaveBeenCalledWith(
      '/jobs/job-example/resumes/resume-example/transformation-plan/approval',
      expect.objectContaining({ method: 'POST', body: JSON.stringify(request) })
    );
  });

  it('4. URL-encodes job and resume identifiers', async () => {
    vi.mocked(apiFetch).mockResolvedValue(response(approval));
    await fetchCVTransformationPlanApproval('job /x', 'resume/#x');
    expect(vi.mocked(apiFetch).mock.calls[0][0]).toBe(
      '/jobs/job%20%2Fx/resumes/resume%2F%23x/transformation-plan/approval'
    );
  });

  it('5. exposes typed 404', async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      response(
        { detail: { code: 'TRANSFORMATION_PLAN_APPROVAL_NOT_FOUND', message: 'Missing' } },
        404
      )
    );
    await expect(fetchCVTransformationPlanApproval('job', 'resume')).rejects.toMatchObject({
      status: 404,
      code: 'TRANSFORMATION_PLAN_APPROVAL_NOT_FOUND',
    });
  });

  it('6. exposes typed stale-plan 409', async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      response({ detail: { code: 'STALE_TRANSFORMATION_PLAN', message: 'Stale' } }, 409)
    );
    await expect(
      saveCVTransformationPlanApproval('job', 'resume', {
        plan_fingerprint: 'a'.repeat(64),
        decisions: [],
        guardrails_acknowledged: false,
        submit: false,
      })
    ).rejects.toMatchObject({ status: 409, code: 'STALE_TRANSFORMATION_PLAN' });
  });

  it('7. exposes typed 422', async () => {
    vi.mocked(apiFetch).mockResolvedValue(
      response({ detail: { code: 'INCOMPLETE_DECISIONS', message: 'Incomplete' } }, 422)
    );
    await expect(
      saveCVTransformationPlanApproval('job', 'resume', {
        plan_fingerprint: 'a'.repeat(64),
        decisions: [],
        guardrails_acknowledged: true,
        submit: true,
      })
    ).rejects.toMatchObject({ status: 422, code: 'INCOMPLETE_DECISIONS' });
  });

  it('8. converts network errors', async () => {
    vi.mocked(apiFetch).mockRejectedValue(new Error('offline'));
    await expect(fetchCVTransformationPlanApproval('job', 'resume')).rejects.toMatchObject({
      status: 0,
      code: 'NETWORK_OR_CONTRACT_ERROR',
    });
  });

  it('9. preserves AbortError', async () => {
    const error = new Error('aborted');
    error.name = 'AbortError';
    vi.mocked(apiFetch).mockRejectedValue(error);
    await expect(fetchCVTransformationPlanApproval('job', 'resume')).rejects.toBe(error);
  });

  it('10. rejects private root fields', () => {
    const value = copy();
    value.metadata_json = { private: true };
    expect(() => parseCVTransformationPlanApproval(value)).toThrow(
      'Unexpected field approval.metadata_json.'
    );
  });

  it('11. rejects invalid decisions', () => {
    const value = copy();
    value.decisions = [{ source_reference: 'resume:summary:0001', decision: 'REPHRASE' }];
    expect(() => parseCVTransformationPlanApproval(value)).toThrow(
      'Invalid transformation-plan decision.'
    );
  });

  it('12. rejects duplicate decision references', () => {
    const value = copy();
    const decision = { source_reference: 'resume:summary:0001', decision: 'ACCEPT' };
    value.decisions = [decision, decision];
    expect(() => parseCVTransformationPlanApproval(value)).toThrow(
      'Approval decisions must be unique and sorted.'
    );
  });

  it('13. rejects unsorted decisions', () => {
    const value = copy();
    value.decisions = [
      { source_reference: 'resume:tools:0001', decision: 'ACCEPT' },
      { source_reference: 'resume:summary:0001', decision: 'REJECT' },
    ];
    expect(() => parseCVTransformationPlanApproval(value)).toThrow(
      'Approval decisions must be unique and sorted.'
    );
  });

  it('14. rejects an invalid fingerprint', () => {
    const value = copy();
    value.plan_fingerprint = 'frontend-calculated';
    expect(() => parseCVTransformationPlanApproval(value)).toThrow('Invalid plan_fingerprint.');
  });

  it('15. rejects an invalid status', () => {
    const value = copy();
    value.status = 'SAVED';
    expect(() => parseCVTransformationPlanApproval(value)).toThrow('Invalid approval status.');
  });

  it('16. exports the dedicated error type', () => {
    expect(new CVTransformationPlanApprovalApiError('TEST', 409, 'test')).toMatchObject({
      name: 'CVTransformationPlanApprovalApiError',
      status: 409,
    });
  });

  it('17. rejects APPROVED without guardrails acknowledgment', () => {
    const value = copy();
    value.guardrails_acknowledged = false;
    expect(() => parseCVTransformationPlanApproval(value)).toThrow(
      'APPROVED requires guardrails acknowledgment.'
    );
  });

  it('18. rejects APPROVED with REQUEST_REVIEW', () => {
    const value = copy();
    value.decisions = [{ source_reference: 'resume:summary:0001', decision: 'REQUEST_REVIEW' }];
    expect(() => parseCVTransformationPlanApproval(value)).toThrow(
      'APPROVED cannot contain REQUEST_REVIEW.'
    );
  });

  it('19. rejects REQUIRES_REVIEW without guardrails acknowledgment', () => {
    const value = copy();
    value.status = 'REQUIRES_REVIEW';
    value.decisions = [{ source_reference: 'resume:summary:0001', decision: 'REQUEST_REVIEW' }];
    value.guardrails_acknowledged = false;
    expect(() => parseCVTransformationPlanApproval(value)).toThrow(
      'REQUIRES_REVIEW requires guardrails acknowledgment.'
    );
  });

  it('20. rejects REQUIRES_REVIEW without REQUEST_REVIEW', () => {
    const value = copy();
    value.status = 'REQUIRES_REVIEW';
    expect(() => parseCVTransformationPlanApproval(value)).toThrow(
      'REQUIRES_REVIEW requires REQUEST_REVIEW.'
    );
  });
});
