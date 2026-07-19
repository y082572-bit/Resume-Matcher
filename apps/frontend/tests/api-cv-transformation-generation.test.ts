import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  fetchCVTransformationGeneration,
  fetchCurrentCVTransformationGeneration,
  generateCVFromApprovedPlan,
  parseCVTransformationGeneration,
} from '@/lib/api/cv-transformation-generation';

const mockFetch = vi.fn();
vi.mock('@/lib/api/client', () => ({ apiFetch: (...args: unknown[]) => mockFetch(...args) }));

const provenance = {
  source_reference: 'resume:summary:0001',
  section: 'SUMMARY',
  action: 'KEEP',
  decision: 'ACCEPT',
  mode: 'COPIED',
  priority: 'NORMAL',
  source_fingerprint: 'b'.repeat(64),
  output_fingerprint: 'c'.repeat(64),
  llm_used: false,
  prompt_version: null,
  validation_status: 'NOT_RUN',
  boundary_validation_status: 'NOT_APPLICABLE',
};

function fixture(status = 'GENERATED') {
  return {
    generation_id: 'generation-example',
    approval_id: 'approval-example',
    resume_id: 'resume-example',
    job_id: 'job-example',
    plan_version: '1.1',
    plan_fingerprint: 'a'.repeat(64),
    generation_version: '1.3',
    prompt_version: '1.3',
    generation_input_fingerprint: 'd'.repeat(64),
    status,
    provider: 'ollama',
    model: 'example-model',
    draft_resume:
      status === 'GENERATED'
        ? {
            personalInfo: {
              name: 'Example Candidate',
              title: '',
              email: '',
              phone: '',
              location: '',
              website: null,
              linkedin: null,
              github: null,
            },
            summary: 'Anonymous summary',
            workExperience: [],
            education: [],
            personalProjects: [],
            additional: {
              technicalSkills: [],
              languages: [],
              certificationsTraining: [],
              awards: [],
            },
            sectionMeta: [],
            customSections: {},
          }
        : null,
    provenance: status === 'GENERATED' ? [provenance] : [],
    failure_code: status === 'FAILED' ? 'LLM_PROVIDER_ERROR' : null,
    attempt_count: 1,
    created_at: '2026-07-19T00:00:00Z',
    updated_at: '2026-07-19T00:01:00Z',
    completed_at: status === 'GENERATING' ? null : '2026-07-19T00:01:00Z',
    requires_truth_validation: true,
    truth_validation_status: 'NOT_RUN',
    applied_to_resume: false,
  };
}

describe('Stage 10C generation client', () => {
  beforeEach(() => mockFetch.mockReset());

  it('01 parses GENERATED', () =>
    expect(parseCVTransformationGeneration(fixture()).status).toBe('GENERATED'));
  it('02 parses GENERATING', () =>
    expect(parseCVTransformationGeneration(fixture('GENERATING')).draft_resume).toBeNull());
  it('03 parses FAILED', () =>
    expect(parseCVTransformationGeneration(fixture('FAILED')).failure_code).toBe(
      'LLM_PROVIDER_ERROR'
    ));
  it('04 parses SUPERSEDED', () =>
    expect(parseCVTransformationGeneration(fixture('SUPERSEDED')).status).toBe('SUPERSEDED'));

  it('05 rejects extra root field', () =>
    expect(() =>
      parseCVTransformationGeneration({ ...fixture(), source_payload: 'private' })
    ).toThrow('Unexpected'));
  it('06 rejects missing root field', () => {
    const value = fixture() as Record<string, unknown>;
    delete value.model;
    expect(() => parseCVTransformationGeneration(value)).toThrow('Missing');
  });
  it('07 rejects unknown status', () =>
    expect(() => parseCVTransformationGeneration({ ...fixture(), status: 'READY' })).toThrow(
      'status'
    ));
  it('08 rejects bad plan fingerprint', () =>
    expect(() =>
      parseCVTransformationGeneration({ ...fixture(), plan_fingerprint: 'bad' })
    ).toThrow('fingerprint'));
  it('09 rejects unsafe flags', () =>
    expect(() =>
      parseCVTransformationGeneration({ ...fixture(), applied_to_resume: true })
    ).toThrow('Unsafe'));
  it('10 rejects GENERATED without draft', () =>
    expect(() => parseCVTransformationGeneration({ ...fixture(), draft_resume: null })).toThrow(
      'GENERATED'
    ));
  it('11 rejects GENERATED without provenance', () =>
    expect(() => parseCVTransformationGeneration({ ...fixture(), provenance: [] })).toThrow(
      'GENERATED'
    ));
  it('12 rejects GENERATED with failure', () =>
    expect(() =>
      parseCVTransformationGeneration({ ...fixture(), failure_code: 'LLM_PROVIDER_ERROR' })
    ).toThrow('GENERATED'));
  it('13 rejects GENERATING with draft', () =>
    expect(() =>
      parseCVTransformationGeneration({ ...fixture(), status: 'GENERATING', completed_at: null })
    ).toThrow('GENERATING'));
  it('14 rejects GENERATING with provenance', () =>
    expect(() =>
      parseCVTransformationGeneration({ ...fixture('GENERATING'), provenance: [provenance] })
    ).toThrow('GENERATING'));
  it('15 rejects FAILED with draft', () =>
    expect(() =>
      parseCVTransformationGeneration({
        ...fixture(),
        status: 'FAILED',
        failure_code: 'LLM_PROVIDER_ERROR',
      })
    ).toThrow('FAILED'));
  it('16 rejects FAILED without code', () =>
    expect(() =>
      parseCVTransformationGeneration({ ...fixture('FAILED'), failure_code: null })
    ).toThrow('FAILED'));
  it('17 rejects SUPERSEDED with draft', () =>
    expect(() => parseCVTransformationGeneration({ ...fixture(), status: 'SUPERSEDED' })).toThrow(
      'SUPERSEDED'
    ));

  it.each([
    ['18 extra provenance field', { ...provenance, raw_truth: 'private' }],
    ['19 invalid section', { ...provenance, section: 'EDUCATION' }],
    ['20 invalid priority', { ...provenance, priority: 'URGENT' }],
    ['21 invalid decision', { ...provenance, decision: 'REQUEST_REVIEW' }],
    ['22 invalid validation status', { ...provenance, validation_status: 'PASSED' }],
    ['23 invalid provenance fingerprint', { ...provenance, source_fingerprint: 'bad' }],
  ])('%s', (_name, invalid) =>
    expect(() => parseCVTransformationGeneration({ ...fixture(), provenance: [invalid] })).toThrow()
  );

  it('24 rejects structurally incomplete draft', () =>
    expect(() =>
      parseCVTransformationGeneration({ ...fixture(), draft_resume: { summary: 'Only summary' } })
    ).toThrow('Missing'));

  it('25 GET current encodes ids and forwards AbortSignal', async () => {
    mockFetch.mockResolvedValue(new Response(JSON.stringify(fixture()), { status: 200 }));
    const controller = new AbortController();
    await fetchCurrentCVTransformationGeneration(
      'job / example',
      'resume / example',
      controller.signal
    );
    expect(mockFetch).toHaveBeenCalledWith(
      '/jobs/job%20%2F%20example/resumes/resume%20%2F%20example/transformation-plan/generation',
      { method: 'GET', signal: controller.signal }
    );
  });

  it('26 GET by id encodes generation id', async () => {
    mockFetch.mockResolvedValue(new Response(JSON.stringify(fixture()), { status: 200 }));
    await fetchCVTransformationGeneration('job', 'resume', 'generation / example');
    expect(mockFetch.mock.calls[0][0]).toContain('/generation%20%2F%20example');
  });

  it('27 POST sends only approval, fingerprint and retry', async () => {
    mockFetch.mockResolvedValue(new Response(JSON.stringify(fixture()), { status: 200 }));
    await generateCVFromApprovedPlan('job', 'resume', {
      approval_id: 'approval-example',
      plan_fingerprint: 'a'.repeat(64),
      retry_failed: true,
    });
    const body = JSON.parse(mockFetch.mock.calls[0][1].body);
    expect(body).toEqual({
      approval_id: 'approval-example',
      plan_fingerprint: 'a'.repeat(64),
      retry_failed: true,
    });
    expect(body).not.toHaveProperty('prompt');
    expect(body).not.toHaveProperty('action');
    expect(body).not.toHaveProperty('model');
  });

  it('28 exposes safe 409 error code', async () => {
    mockFetch.mockResolvedValue(
      new Response(
        JSON.stringify({ detail: { code: 'GENERATION_IN_PROGRESS', message: 'In progress' } }),
        { status: 409 }
      )
    );
    await expect(fetchCurrentCVTransformationGeneration('job', 'resume')).rejects.toMatchObject({
      code: 'GENERATION_IN_PROGRESS',
      status: 409,
    });
  });

  it('29 propagates AbortError', async () => {
    const error = new Error('aborted');
    error.name = 'AbortError';
    mockFetch.mockImplementationOnce(() => {
      throw error;
    });
    try {
      await fetchCurrentCVTransformationGeneration('job', 'resume');
      throw new Error('request unexpectedly succeeded');
    } catch (caught) {
      expect((caught as Error).name).toBe('AbortError');
    }
  });

  it('30 maps network and contract errors', async () => {
    mockFetch.mockImplementationOnce(() => {
      throw new Error('offline');
    });
    try {
      await fetchCurrentCVTransformationGeneration('job', 'resume');
      throw new Error('request unexpectedly succeeded');
    } catch (caught) {
      expect((caught as Error).name).toBe('CVTransformationGenerationApiError');
    }
  });

  it.each([
    [
      'unknown personalInfo field',
      () => {
        const value = fixture();
        (value.draft_resume!.personalInfo as Record<string, unknown>).private = 'secret';
        return value;
      },
    ],
    [
      'unknown additional field',
      () => {
        const value = fixture();
        (value.draft_resume!.additional as Record<string, unknown>).competencies = [];
        return value;
      },
    ],
    [
      'unknown experience field',
      () => {
        const value = fixture();
        value.draft_resume!.workExperience.push({
          id: 1,
          title: '',
          company: '',
          location: null,
          years: '',
          description: [],
          private: true,
        } as never);
        return value;
      },
    ],
  ])('R1 rejects %s', (_name, build) =>
    expect(() => parseCVTransformationGeneration(build())).toThrow('Unexpected')
  );

  it.each([
    ['unknown action', { ...provenance, action: 'CREATE' }],
    ['unknown mode', { ...provenance, mode: 'MAGIC' }],
    ['invalid source reference', { ...provenance, source_reference: 'resume:summary:1' }],
    ['copied with LLM', { ...provenance, llm_used: true }],
    ['copied with prompt', { ...provenance, prompt_version: '1.3' }],
    ['copied without output', { ...provenance, output_fingerprint: null }],
    ['reject not excluded', { ...provenance, decision: 'REJECT' }],
  ])('R1 rejects %s', (_name, invalid) =>
    expect(() => parseCVTransformationGeneration({ ...fixture(), provenance: [invalid] })).toThrow()
  );

  it('R1 rejects duplicate provenance', () =>
    expect(() =>
      parseCVTransformationGeneration({ ...fixture(), provenance: [provenance, provenance] })
    ).toThrow('unique'));

  it('R1 rejects unsorted provenance', () => {
    const later = { ...provenance, source_reference: 'resume:tools:0001' };
    expect(() =>
      parseCVTransformationGeneration({ ...fixture(), provenance: [later, provenance] })
    ).toThrow('sorted');
  });

  it('R1 rejects timezone-naive timestamps', () =>
    expect(() =>
      parseCVTransformationGeneration({ ...fixture(), created_at: '2026-07-19T00:00:00' })
    ).toThrow('timezone-aware'));

  it('R1 rejects unknown safe failure code', () =>
    expect(() =>
      parseCVTransformationGeneration({ ...fixture('FAILED'), failure_code: 'PROVIDER_RAW_ERROR' })
    ).toThrow('failure_code'));

  it('R3 accepts protected narrative provenance and rejects it for KEEP', () => {
    const protectedItem = {
      ...provenance,
      action: 'REPHRASE',
      mode: 'COPIED_PROTECTED_CONTENT',
    };
    expect(
      parseCVTransformationGeneration({ ...fixture(), provenance: [protectedItem] }).provenance[0]
        .mode
    ).toBe('COPIED_PROTECTED_CONTENT');
    expect(() =>
      parseCVTransformationGeneration({
        ...fixture(),
        provenance: [{ ...protectedItem, action: 'KEEP' }],
      })
    ).toThrow('matrix');
  });

  it.each([
    [
      'description string',
      () => {
        const value = fixture();
        value.draft_resume!.workExperience.push({
          id: 1,
          title: 'Role',
          company: 'Company',
          location: null,
          years: '2020',
          description: 'bad',
        } as never);
        return value;
      },
    ],
    [
      'title array',
      () => {
        const value = fixture();
        value.draft_resume!.workExperience.push({
          id: 1,
          title: [],
          company: 'Company',
          location: null,
          years: '2020',
          description: [],
        } as never);
        return value;
      },
    ],
    [
      'id object',
      () => {
        const value = fixture();
        value.draft_resume!.workExperience.push({
          id: {},
          title: 'Role',
          company: 'Company',
          location: null,
          years: '2020',
          description: [],
        } as never);
        return value;
      },
    ],
    [
      'technicalSkills number',
      () => {
        const value = fixture();
        value.draft_resume!.additional.technicalSkills.push(7 as never);
        return value;
      },
    ],
    [
      'sectionMeta order string',
      () => {
        const value = fixture();
        value.draft_resume!.sectionMeta.push({
          id: 'x',
          key: 'x',
          displayName: 'x',
          sectionType: 'text',
          isDefault: false,
          isVisible: true,
          order: '1',
        } as never);
        return value;
      },
    ],
    [
      'custom strings string',
      () => {
        const value = fixture();
        (value.draft_resume!.customSections as Record<string, unknown>).x = {
          sectionType: 'stringList',
          items: null,
          strings: 'bad',
          text: null,
        } as never;
        return value;
      },
    ],
    [
      'custom item description object',
      () => {
        const value = fixture();
        (value.draft_resume!.customSections as Record<string, unknown>).x = {
          sectionType: 'itemList',
          items: [
            { id: 1, title: 'x', subtitle: null, location: null, years: '', description: {} },
          ],
          strings: null,
          text: null,
        } as never;
        return value;
      },
    ],
  ])('R2 rejects nested ResumeData %s', (_name, build) =>
    expect(() => parseCVTransformationGeneration(build())).toThrow()
  );

  it.each([
    ['KEEP/COPIED', provenance],
    [
      'DEEMPHASIZE/COPIED_LOW_PRIORITY',
      { ...provenance, action: 'DEEMPHASIZE', mode: 'COPIED_LOW_PRIORITY', priority: 'LOW' },
    ],
    [
      'OMIT/OMITTED',
      { ...provenance, action: 'OMIT', mode: 'OMITTED', priority: 'LOW', output_fingerprint: null },
    ],
    [
      'HUMAN_REVIEW/PRESERVED',
      { ...provenance, action: 'HUMAN_REVIEW', mode: 'PRESERVED_AFTER_REVIEW' },
    ],
    [
      'HUMAN_REVIEW/UNRESOLVED',
      {
        ...provenance,
        action: 'HUMAN_REVIEW',
        mode: 'EXCLUDED_UNRESOLVED',
        output_fingerprint: null,
      },
    ],
    [
      'SUMMARY/GENERATED',
      {
        ...provenance,
        action: 'REPHRASE',
        mode: 'GENERATED',
        llm_used: true,
        prompt_version: '1.3',
        boundary_validation_status: 'PASSED',
      },
    ],
    [
      'EXPERIENCE/EMPTY',
      {
        ...provenance,
        section: 'EXPERIENCE',
        action: 'EMPHASIZE',
        mode: 'COPIED_NO_MUTABLE_CONTENT',
        priority: 'HIGH',
      },
    ],
    [
      'COMPETENCIES/EXACT',
      {
        ...provenance,
        section: 'COMPETENCIES',
        action: 'EMPHASIZE',
        mode: 'EXACT_INSERT',
        priority: 'HIGH',
      },
    ],
    [
      'ACHIEVEMENT/PARENT',
      {
        ...provenance,
        section: 'ACHIEVEMENTS',
        action: 'EMPHASIZE',
        mode: 'EXCLUDED_BY_PARENT',
        priority: 'HIGH',
        output_fingerprint: null,
      },
    ],
    [
      'REJECT/EXCLUDED',
      { ...provenance, decision: 'REJECT', mode: 'EXCLUDED', output_fingerprint: null },
    ],
  ])('R2 accepts action/mode matrix %s', (_name, valid) =>
    expect(
      parseCVTransformationGeneration({ ...fixture(), provenance: [valid] }).provenance[0].mode
    ).toBe(valid.mode)
  );

  it.each([
    ['generation_version', '1.1'],
    ['prompt_version', '1.1'],
    ['plan_version', '1.0'],
  ])('R2 rejects unsupported %s', (field, value) =>
    expect(() => parseCVTransformationGeneration({ ...fixture(), [field]: value })).toThrow(
      'Unsupported'
    )
  );
});
