import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import ApprovedPlanGenerationPage from '@/app/(default)/resumes/[id]/positioning/plan/generation/page';
import * as careerApi from '@/lib/api/career-positioning';
import * as planApi from '@/lib/api/cv-transformation-plan';
import * as approvalApi from '@/lib/api/cv-transformation-approval';
import * as generationApi from '@/lib/api/cv-transformation-generation';
import type { CVTransformationPlan } from '@/lib/types/cv-transformation-plan';
import type { CVTransformationPlanApproval } from '@/lib/types/cv-transformation-approval';
import type { CVTransformationGeneration } from '@/lib/types/cv-transformation-generation';

const push = vi.fn();
vi.mock('next/navigation', () => ({
  useParams: () => ({ id: 'resume-example' }),
  useRouter: () => ({ push }),
}));
vi.mock('@/lib/api/career-positioning', async (original) => ({
  ...(await original<typeof import('@/lib/api/career-positioning')>()),
  fetchResumePositioningContext: vi.fn(),
  fetchResumeJobContext: vi.fn(),
}));
vi.mock('@/lib/api/cv-transformation-plan', async (original) => ({
  ...(await original<typeof import('@/lib/api/cv-transformation-plan')>()),
  fetchCVTransformationPlan: vi.fn(),
}));
vi.mock('@/lib/api/cv-transformation-approval', async (original) => ({
  ...(await original<typeof import('@/lib/api/cv-transformation-approval')>()),
  fetchCVTransformationPlanApproval: vi.fn(),
}));
vi.mock('@/lib/api/cv-transformation-generation', async (original) => ({
  ...(await original<typeof import('@/lib/api/cv-transformation-generation')>()),
  fetchCurrentCVTransformationGeneration: vi.fn(),
  generateCVFromApprovedPlan: vi.fn(),
}));

const plan = {
  plan_version: '1.1',
  plan_fingerprint: 'a'.repeat(64),
  resume_id: 'resume-example',
  job_id: 'job-example',
  generated_at: '2026-07-19T00:00:00Z',
  detected_job_level: 'SPECIALIST',
  candidate_profile_level: 'SPECIALIST',
  positioning_strategy: 'BALANCED',
  requires_human_review: false,
  summary_plan: [
    {
      action: 'REPHRASE',
      section: 'SUMMARY',
      source_reference: 'resume:summary:0001',
      display_label: 'Profil zawodowy',
      reason_code: 'TEST',
      reason: 'Test',
      evidence_strength: 'STRONG',
      human_review_required: false,
    },
  ],
  experience_plan: [],
  competencies_plan: [],
  achievements_plan: [],
  tools_plan: [],
  evidence_permissions: [],
  prohibited_claims: [],
  limitations: [],
  source_summary: {
    approved_truth_items_count: 0,
    resume_items_count: 0,
    job_requirements_count: 1,
    career_positioning_schema_version: '1.0',
  },
} as CVTransformationPlan;

function approval(
  status: CVTransformationPlanApproval['status'] = 'APPROVED'
): CVTransformationPlanApproval {
  return {
    approval_id: 'approval-example',
    plan_version: '1.1',
    plan_fingerprint: plan.plan_fingerprint,
    resume_id: plan.resume_id,
    job_id: plan.job_id,
    status,
    decisions: [],
    guardrails_acknowledged: status !== 'DRAFT',
    created_at: '2026-07-19T00:00:00Z',
    updated_at: '2026-07-19T00:00:00Z',
  };
}

function generation(
  status: CVTransformationGeneration['status'] = 'GENERATED'
): CVTransformationGeneration {
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
            personalInfo: { name: 'Example Candidate' },
            summary: 'Generated anonymous summary',
            workExperience: [
              {
                id: 1,
                title: 'Engineer',
                company: 'Example Company',
                years: '2020-2024',
                description: ['Built reliable systems'],
              },
            ],
            education: [],
            personalProjects: [],
            additional: {
              technicalSkills: ['Python'],
              languages: ['English'],
              certificationsTraining: ['Example Certificate'],
              awards: [],
            },
            sectionMeta: [
              {
                id: 'system:approved-plan-generation:careerPositioningCompetencies',
                key: 'careerPositioningCompetencies',
                displayName: 'Kluczowe kompetencje',
                sectionType: 'stringList',
                isDefault: false,
                isVisible: true,
                order: 6,
              },
            ],
            customSections: {
              careerPositioningCompetencies: {
                sectionType: 'stringList',
                strings: ['Systems thinking'],
              },
            },
          }
        : null,
    provenance:
      status === 'GENERATED'
        ? [
            {
              source_reference: 'resume:summary:0001',
              section: 'SUMMARY',
              action: 'REPHRASE',
              decision: 'ACCEPT',
              mode: 'GENERATED',
              priority: 'NORMAL',
              source_fingerprint: 'b'.repeat(64),
              output_fingerprint: 'c'.repeat(64),
              llm_used: true,
              prompt_version: '1.3',
              validation_status: 'NOT_RUN',
              boundary_validation_status: 'PASSED',
            },
          ]
        : [],
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

const positioning = vi.mocked(careerApi.fetchResumePositioningContext);
const context = vi.mocked(careerApi.fetchResumeJobContext);
const fetchPlan = vi.mocked(planApi.fetchCVTransformationPlan);
const fetchApproval = vi.mocked(approvalApi.fetchCVTransformationPlanApproval);
const fetchGeneration = vi.mocked(generationApi.fetchCurrentCVTransformationGeneration);
const generate = vi.mocked(generationApi.generateCVFromApprovedPlan);

function install(
  current: CVTransformationGeneration | null = null,
  status: CVTransformationPlanApproval['status'] = 'APPROVED'
) {
  positioning.mockResolvedValue({} as never);
  context.mockResolvedValue({ job_id: 'job-example', job_title: 'Example role' });
  fetchPlan.mockResolvedValue(plan);
  fetchApproval.mockResolvedValue(approval(status));
  if (current) fetchGeneration.mockResolvedValue(current);
  else
    fetchGeneration.mockRejectedValue(
      new generationApi.CVTransformationGenerationApiError(
        'TRANSFORMATION_GENERATION_NOT_FOUND',
        404,
        'missing'
      )
    );
}

describe('Stage 10C generation page', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    install();
  });
  afterEach(() => vi.restoreAllMocks());

  it('01 renders loading states for plan and approval', () => {
    render(<ApprovedPlanGenerationPage />);
    expect(screen.getByText(/Ładowanie planu/)).toBeDefined();
  });

  it('02 renders approved empty state and generate action', async () => {
    render(<ApprovedPlanGenerationPage />);
    expect(await screen.findByText('Brak draftu')).toBeDefined();
    expect(screen.getByRole('button', { name: 'Generuj draft' })).toBeDefined();
  });

  it('03 sends the safe generation request once', async () => {
    generate.mockResolvedValue(generation());
    render(<ApprovedPlanGenerationPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Generuj draft' }));
    await waitFor(() => expect(generate).toHaveBeenCalledTimes(1));
    expect(generate).toHaveBeenCalledWith(
      'job-example',
      'resume-example',
      { approval_id: 'approval-example', plan_fingerprint: 'a'.repeat(64), retry_failed: false },
      expect.any(AbortSignal)
    );
  });

  it('04 renders generated summary', async () => {
    install(generation());
    render(<ApprovedPlanGenerationPage />);
    expect(await screen.findByText('Generated anonymous summary')).toBeDefined();
  });
  it('05 renders experience and achievements', async () => {
    install(generation());
    render(<ApprovedPlanGenerationPage />);
    expect(await screen.findByText('Built reliable systems')).toBeDefined();
  });
  it('06 renders competencies and tools', async () => {
    install(generation());
    render(<ApprovedPlanGenerationPage />);
    expect(await screen.findByText('Python')).toBeDefined();
    expect(screen.getByText('Systems thinking')).toBeDefined();
    expect(screen.getByRole('heading', { name: 'Kluczowe kompetencje' })).toBeDefined();
    expect(screen.getByRole('heading', { name: 'Narzędzia techniczne' })).toBeDefined();
  });
  it('07 renders provenance without source payload', async () => {
    install(generation());
    render(<ApprovedPlanGenerationPage />);
    expect(await screen.findByText(/resume:summary:0001/)).toBeDefined();
    expect(screen.queryByText('source_payload')).toBeNull();
  });
  it('08 renders AI warning and NOT_RUN truth status', async () => {
    install(generation());
    render(<ApprovedPlanGenerationPage />);
    expect(await screen.findByText(/wygenerowany przez AI/)).toBeDefined();
    expect(screen.getByText('NOT_RUN')).toBeDefined();
  });

  it('09 exposes no apply, save, export or editing control', async () => {
    install(generation());
    render(<ApprovedPlanGenerationPage />);
    await screen.findByText('Generated anonymous summary');
    expect(screen.queryByRole('button', { name: /Apply|Zapisz Resume|Export/i })).toBeNull();
    expect(document.querySelector('textarea')).toBeNull();
  });

  it('10 FAILED exposes explicit retry_failed action', async () => {
    install(generation('FAILED'));
    generate.mockResolvedValue(generation());
    render(<ApprovedPlanGenerationPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Ponów nieudaną generację' }));
    await waitFor(() =>
      expect(generate).toHaveBeenCalledWith(
        'job-example',
        'resume-example',
        expect.objectContaining({ retry_failed: true }),
        expect.any(AbortSignal)
      )
    );
  });

  it('11 GENERATING has refresh without automatic polling', async () => {
    install(generation('GENERATING'));
    render(<ApprovedPlanGenerationPage />);
    expect(await screen.findByRole('button', { name: 'Odśwież status' })).toBeDefined();
    expect(fetchGeneration).toHaveBeenCalledTimes(1);
  });

  it.each(['DRAFT', 'REQUIRES_REVIEW', 'SUPERSEDED'] as const)(
    '%s approval blocks generation',
    async (status) => {
      install(null, status);
      render(<ApprovedPlanGenerationPage />);
      expect(await screen.findByText(/wyłącznie dla aktualnego approval/)).toBeDefined();
      expect(screen.queryByRole('button', { name: 'Generuj draft' })).toBeNull();
    }
  );

  it.each([
    ['12 stale 409', 409, 'STALE_TRANSFORMATION_PLAN', /zmieniło się/],
    ['13 unsafe 502', 502, 'INVALID_LLM_RESPONSE', /niebezpieczną/],
    ['14 unconfigured 503', 503, 'LLM_NOT_CONFIGURED', /Skonfiguruj provider/],
  ])('%s', async (_name, status, code, expected) => {
    generate.mockRejectedValue(
      new generationApi.CVTransformationGenerationApiError(code as string, status as number, 'safe')
    );
    render(<ApprovedPlanGenerationPage />);
    fireEvent.click(await screen.findByRole('button', { name: 'Generuj draft' }));
    expect(await screen.findByText(expected as RegExp)).toBeDefined();
  });

  it('15 supports keyboard back navigation and keeps Stage 10D disabled', async () => {
    install(generation());
    render(<ApprovedPlanGenerationPage />);
    const back = await screen.findAllByRole('button', { name: 'Wróć do planu' });
    fireEvent.keyDown(back[0], { key: 'Enter' });
    fireEvent.click(back[0]);
    expect(push).toHaveBeenCalledWith('/resumes/resume-example/positioning/plan');
    expect(screen.getByRole('button', { name: /Etap 10D/ })).toBeDisabled();
  });

  it('R1 renders job title, prompt version, shortened ids and per-item statuses', async () => {
    install(generation());
    render(<ApprovedPlanGenerationPage />);
    expect(await screen.findByText('Example role')).toBeDefined();
    expect(screen.getByText('1.3')).toBeDefined();
    expect(screen.getByText('aaaaaaaaaaaa')).toBeDefined();
    expect(screen.getByText('approval-exa')).toBeDefined();
    expect(screen.getByText('Profil zawodowy')).toBeDefined();
    expect(screen.getByText(/Truth validation: NOT_RUN/)).toBeDefined();
    expect(screen.getByText(/Boundary validation: PASSED/)).toBeDefined();
  });

  it('R3 explains protected source copying without presenting a generation error', async () => {
    const protectedGeneration = generation();
    protectedGeneration.provenance[0] = {
      ...protectedGeneration.provenance[0],
      action: 'REPHRASE',
      mode: 'COPIED_PROTECTED_CONTENT',
      llm_used: false,
      prompt_version: null,
      boundary_validation_status: 'NOT_APPLICABLE',
    };
    install(protectedGeneration);
    render(<ApprovedPlanGenerationPage />);
    expect(await screen.findByText(/Zachowano dokładną treść źródłową/)).toBeDefined();
    expect(screen.queryByText(/Generowanie zakończyło się błędem/)).toBeNull();
  });

  it('R1 handles approval 404 explicitly', async () => {
    fetchApproval.mockRejectedValue(
      new approvalApi.CVTransformationPlanApprovalApiError('NOT_FOUND', 404, 'missing')
    );
    render(<ApprovedPlanGenerationPage />);
    expect(await screen.findByText('Nie znaleziono approval dla aktualnego planu.')).toBeDefined();
    expect(screen.queryByRole('button', { name: 'Generuj draft' })).toBeNull();
  });

  it('R1 never presents an old draft marked SUPERSEDED', async () => {
    const stale = { ...generation(), status: 'SUPERSEDED' } as CVTransformationGeneration;
    install(stale);
    render(<ApprovedPlanGenerationPage />);
    expect(await screen.findByText('Status: SUPERSEDED')).toBeDefined();
    expect(screen.queryByText('Generated anonymous summary')).toBeNull();
  });

  it('R1 blocks generation for an empty plan', async () => {
    install();
    fetchPlan.mockResolvedValue({ ...plan, summary_plan: [] });
    render(<ApprovedPlanGenerationPage />);
    expect(await screen.findByText('Plan nie zawiera elementów do generacji.')).toBeDefined();
    expect(screen.queryByRole('button', { name: 'Generuj draft' })).toBeNull();
  });

  it('R2 hides an old draft during refresh and keeps it hidden after a network error', async () => {
    install(generation());
    render(<ApprovedPlanGenerationPage />);
    expect(await screen.findByText('Generated anonymous summary')).toBeDefined();

    let rejectRefresh!: (reason: unknown) => void;
    positioning.mockImplementationOnce(
      () =>
        new Promise((_, reject) => {
          rejectRefresh = reject;
        }) as never
    );
    fireEvent.click(screen.getByRole('button', { name: 'Odśwież' }));
    await waitFor(() => expect(screen.queryByText('Generated anonymous summary')).toBeNull());
    rejectRefresh(
      new generationApi.CVTransformationGenerationApiError('LLM_NOT_CONFIGURED', 503, 'safe')
    );
    expect(await screen.findByText(/Skonfiguruj provider/)).toBeDefined();
    expect(screen.queryByText('Generated anonymous summary')).toBeNull();
    expect(screen.getByRole('button', { name: 'Generuj draft' })).toBeDisabled();
  });

  it('R2 identifies only collision-safe system competency metadata', async () => {
    const value = generation();
    value.draft_resume!.sectionMeta = [
      {
        id: 'user-section',
        key: 'careerPositioningCompetencies',
        displayName: 'Kluczowe kompetencje',
        sectionType: 'stringList',
        isDefault: false,
        isVisible: true,
        order: 5,
      },
      {
        id: 'system:approved-plan-generation:careerPositioningCompetencies2',
        key: 'careerPositioningCompetencies2',
        displayName: 'Kluczowe kompetencje',
        sectionType: 'stringList',
        isDefault: false,
        isVisible: true,
        order: 6,
      },
    ];
    value.draft_resume!.customSections = {
      careerPositioningCompetencies: {
        sectionType: 'stringList',
        strings: ['User-only competency'],
      },
      careerPositioningCompetencies2: {
        sectionType: 'stringList',
        strings: ['System competency'],
      },
    };
    install(value);
    render(<ApprovedPlanGenerationPage />);
    expect(await screen.findByText('System competency')).toBeDefined();
    expect(screen.queryByText('User-only competency')).toBeNull();
  });
});
