import React from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

import TransformationPlanReviewPage from '@/app/(default)/resumes/[id]/positioning/plan/page';
import * as careerApi from '@/lib/api/career-positioning';
import * as planApi from '@/lib/api/cv-transformation-plan';
import * as approvalApi from '@/lib/api/cv-transformation-approval';
import type { CVTransformationPlan } from '@/lib/types/cv-transformation-plan';
import type { CVTransformationPlanApproval } from '@/lib/types/cv-transformation-approval';

const push = vi.fn();
vi.mock('next/navigation', () => ({
  useParams: () => ({ id: 'resume-example' }),
  useRouter: () => ({ push }),
}));

vi.mock('@/lib/api/career-positioning', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api/career-positioning')>();
  return {
    ...actual,
    fetchResumePositioningContext: vi.fn(),
    fetchResumeJobContext: vi.fn(),
  };
});

vi.mock('@/lib/api/cv-transformation-plan', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api/cv-transformation-plan')>();
  return { ...actual, fetchCVTransformationPlan: vi.fn() };
});

vi.mock('@/lib/api/cv-transformation-approval', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/api/cv-transformation-approval')>();
  return {
    ...actual,
    fetchCVTransformationPlanApproval: vi.fn(),
    saveCVTransformationPlanApproval: vi.fn(),
  };
});

const guardrailCodes = [
  'BOARD_WITHOUT_EVIDENCE',
  'BUDGET_WITHOUT_EVIDENCE',
  'CERTIFICATION_WITHOUT_EVIDENCE',
  'LANGUAGE_LEVEL_WITHOUT_EVIDENCE',
  'MANAGING_MANAGERS_WITHOUT_EVIDENCE',
  'PEOPLE_MANAGEMENT_WITHOUT_EVIDENCE',
  'PNL_WITHOUT_EVIDENCE',
  'QUANTIFIED_RESULT_WITHOUT_EVIDENCE',
  'TEAM_SIZE_WITHOUT_EVIDENCE',
  'TECHNICAL_SKILL_WITHOUT_EVIDENCE',
] as const;

const plan: CVTransformationPlan = {
  plan_version: '1.1',
  plan_fingerprint: 'a'.repeat(64),
  resume_id: 'resume-example',
  job_id: 'job-example',
  generated_at: '2026-07-19T10:00:00Z',
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
      reason_code: 'REVIEW',
      reason: 'Review positioning carefully.',
      evidence_strength: 'WEAK',
      human_review_required: true,
    },
  ],
  experience_plan: [],
  competencies_plan: [],
  achievements_plan: [],
  tools_plan: [
    {
      action: 'KEEP',
      section: 'TOOLS',
      source_reference: 'resume:tools:0001',
      display_label: 'Python',
      reason_code: 'KEEP_TOOL',
      reason: 'Keep verified tool.',
      evidence_strength: 'STRONG',
      human_review_required: false,
    },
  ],
  evidence_permissions: [
    {
      claim_code: 'TECHNICAL_SKILL_WITHOUT_EVIDENCE',
      source_reference: 'resume:tools:0001',
      evidence_strength: 'STRONG',
      allowed_operation: 'KEEP_EXISTING',
      human_review_required: false,
    },
  ],
  prohibited_claims: guardrailCodes.map((code) => ({
    code,
    reason_code: 'EVIDENCE_REQUIRED_FOR_NEW_CLAIM',
    reason: `Guardrail ${code}`,
    human_review_required: true,
  })),
  limitations: ['No generated text in Stage 10B.'],
  source_summary: {
    approved_truth_items_count: 2,
    resume_items_count: 2,
    job_requirements_count: 3,
    career_positioning_schema_version: '1.0',
  },
};

function approval(status: CVTransformationPlanApproval['status']): CVTransformationPlanApproval {
  return {
    approval_id: 'approval-example',
    plan_version: '1.1',
    plan_fingerprint: plan.plan_fingerprint,
    resume_id: plan.resume_id,
    job_id: plan.job_id,
    status,
    decisions: [
      { source_reference: 'resume:summary:0001', decision: 'ACCEPT' },
      { source_reference: 'resume:tools:0001', decision: 'ACCEPT' },
    ],
    guardrails_acknowledged: true,
    created_at: '2026-07-19T10:00:00Z',
    updated_at: '2026-07-19T10:00:00Z',
  };
}

async function renderLoaded() {
  render(<TransformationPlanReviewPage />);
  await screen.findByRole('heading', { name: 'Plan transformacji CV' });
}

function completeSelections(value: 'Akceptuję' | 'Odrzucam' | 'Proszę o przegląd' = 'Akceptuję') {
  screen.getAllByLabelText(new RegExp(`^${value}`)).forEach((radio) => fireEvent.click(radio));
  fireEvent.click(
    screen.getByLabelText(
      'Rozumiem, że generator nie może tworzyć twierdzeń bez odpowiedniego dowodu.'
    )
  );
}

describe('TransformationPlanReviewPage', () => {
  let consoleSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.clearAllMocks();
    consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.mocked(careerApi.fetchResumePositioningContext).mockResolvedValue({
      resume_id: 'resume-example',
      parent_id: 'parent-example',
    });
    vi.mocked(careerApi.fetchResumeJobContext).mockResolvedValue({
      job_id: 'job-example',
      job_title: 'Example Engineering Manager',
    });
    vi.mocked(planApi.fetchCVTransformationPlan).mockResolvedValue(plan);
    vi.mocked(approvalApi.fetchCVTransformationPlanApproval).mockRejectedValue(
      new approvalApi.CVTransformationPlanApprovalApiError('NOT_FOUND', 404, 'Missing')
    );
    vi.mocked(approvalApi.saveCVTransformationPlanApproval).mockResolvedValue(approval('APPROVED'));
  });

  afterEach(() => consoleSpy.mockRestore());

  it('1. renders a loading state', () => {
    vi.mocked(careerApi.fetchResumePositioningContext).mockReturnValue(new Promise(() => {}));
    render(<TransformationPlanReviewPage />);
    expect(screen.getByRole('status').textContent).toContain('Ładowanie');
  });

  it('2. performs the three required core requests', async () => {
    await renderLoaded();
    expect(careerApi.fetchResumePositioningContext).toHaveBeenCalledTimes(1);
    expect(careerApi.fetchResumeJobContext).toHaveBeenCalledTimes(1);
    expect(planApi.fetchCVTransformationPlan).toHaveBeenCalledWith(
      'job-example',
      'resume-example',
      expect.any(AbortSignal)
    );
  });

  it('3. renders title, levels, strategy and plan version', async () => {
    await renderLoaded();
    expect(screen.getByText('Example Engineering Manager')).toBeDefined();
    expect(screen.getByText('MANAGER')).toBeDefined();
    expect(screen.getByText('EXPERT')).toBeDefined();
    expect(screen.getByText('CONTROLLED_ELEVATION')).toBeDefined();
    expect(screen.getByText('1.1')).toBeDefined();
  });

  it('4. renders plan sections', async () => {
    await renderLoaded();
    expect(screen.getByRole('heading', { name: 'Profil' })).toBeDefined();
    expect(screen.getByRole('heading', { name: 'Narzędzia' })).toBeDefined();
  });

  it('5. renders display labels', async () => {
    await renderLoaded();
    expect(screen.getByText('Professional summary')).toBeDefined();
    expect(screen.getByText('Python')).toBeDefined();
  });

  it('6. renders action and reason without changing them', async () => {
    await renderLoaded();
    expect(screen.getByText(/Akcja: HUMAN_REVIEW/)).toBeDefined();
    expect(screen.getByText('Review positioning carefully.')).toBeDefined();
  });

  it('7. renders item-scoped EvidencePermission', async () => {
    await renderLoaded();
    expect(screen.getByText(/TECHNICAL_SKILL_WITHOUT_EVIDENCE \/ KEEP_EXISTING/)).toBeDefined();
  });

  it('8. renders all ten universal guardrails', async () => {
    await renderLoaded();
    guardrailCodes.forEach((code) => {
      expect(screen.getByText(code, { selector: 'strong' })).toBeDefined();
    });
  });

  it('9. creates no automatic decision', async () => {
    await renderLoaded();
    const radios = screen.getAllByRole('radio') as HTMLInputElement[];
    expect(radios.every((radio) => !radio.checked)).toBe(true);
  });

  it('10. supports ACCEPT', async () => {
    await renderLoaded();
    fireEvent.click(screen.getAllByLabelText(/^Akceptuję/)[0]);
    expect((screen.getAllByLabelText(/^Akceptuję/)[0] as HTMLInputElement).checked).toBe(true);
  });

  it('11. supports REJECT', async () => {
    await renderLoaded();
    fireEvent.click(screen.getAllByLabelText(/^Odrzucam/)[0]);
    expect((screen.getAllByLabelText(/^Odrzucam/)[0] as HTMLInputElement).checked).toBe(true);
  });

  it('12. supports REQUEST_REVIEW', async () => {
    await renderLoaded();
    fireEvent.click(screen.getAllByLabelText(/^Proszę o przegląd/)[0]);
    expect((screen.getAllByLabelText(/^Proszę o przegląd/)[0] as HTMLInputElement).checked).toBe(
      true
    );
  });

  it('13. clearly labels HUMAN_REVIEW items', async () => {
    await renderLoaded();
    expect(screen.getByText('Wymaga jawnego przeglądu człowieka')).toBeDefined();
  });

  it('14. disables approval while decisions are missing', async () => {
    await renderLoaded();
    expect(
      (screen.getByRole('button', { name: 'Zatwierdź plan' }) as HTMLButtonElement).disabled
    ).toBe(true);
  });

  it('15. guardrail acknowledgment alone does not enable approval', async () => {
    await renderLoaded();
    fireEvent.click(screen.getByRole('checkbox'));
    expect(
      (screen.getByRole('button', { name: 'Zatwierdź plan' }) as HTMLButtonElement).disabled
    ).toBe(true);
  });

  it('16. complete decisions and acknowledgment enable approval', async () => {
    await renderLoaded();
    completeSelections();
    expect(
      (screen.getByRole('button', { name: 'Zatwierdź plan' }) as HTMLButtonElement).disabled
    ).toBe(false);
  });

  it('17. saves a partial draft explicitly', async () => {
    await renderLoaded();
    fireEvent.click(screen.getAllByLabelText(/^Odrzucam/)[0]);
    fireEvent.click(screen.getByRole('button', { name: 'Zapisz wersję roboczą' }));
    await waitFor(() => expect(approvalApi.saveCVTransformationPlanApproval).toHaveBeenCalled());
    expect(vi.mocked(approvalApi.saveCVTransformationPlanApproval).mock.calls[0][2]).toMatchObject({
      submit: false,
      guardrails_acknowledged: false,
    });
  });

  it('18. shows APPROVED only after backend response', async () => {
    await renderLoaded();
    completeSelections();
    fireEvent.click(screen.getByRole('button', { name: 'Zatwierdź plan' }));
    await screen.findByText('APPROVED');
  });

  it('19. shows REQUIRES_REVIEW returned by backend', async () => {
    vi.mocked(approvalApi.saveCVTransformationPlanApproval).mockResolvedValue(
      approval('REQUIRES_REVIEW')
    );
    await renderLoaded();
    completeSelections('Proszę o przegląd');
    fireEvent.click(screen.getByRole('button', { name: 'Zatwierdź plan' }));
    await screen.findByText('REQUIRES_REVIEW');
  });

  it('20. refreshes the plan only after explicit button action', async () => {
    await renderLoaded();
    fireEvent.click(screen.getByRole('button', { name: 'Odśwież plan' }));
    await waitFor(() => expect(planApi.fetchCVTransformationPlan).toHaveBeenCalledTimes(2));
  });

  it('21. handles 409 and preserves local decisions', async () => {
    vi.mocked(approvalApi.saveCVTransformationPlanApproval).mockRejectedValue(
      new approvalApi.CVTransformationPlanApprovalApiError(
        'STALE_TRANSFORMATION_PLAN',
        409,
        'Stale'
      )
    );
    await renderLoaded();
    completeSelections();
    fireEvent.click(screen.getByRole('button', { name: 'Zatwierdź plan' }));
    await screen.findByText(/Plan zmienił się/);
    expect((screen.getAllByLabelText(/^Akceptuję/)[0] as HTMLInputElement).checked).toBe(true);
  });

  it('22. exposes backend 422 through aria-live validation', async () => {
    vi.mocked(approvalApi.saveCVTransformationPlanApproval).mockRejectedValue(
      new approvalApi.CVTransformationPlanApprovalApiError('INCOMPLETE_DECISIONS', 422, 'Bad')
    );
    await renderLoaded();
    completeSelections();
    fireEvent.click(screen.getByRole('button', { name: 'Zatwierdź plan' }));
    expect(await screen.findByRole('alert')).toHaveAttribute('aria-live', 'assertive');
  });

  it('23. renders a 404 state', async () => {
    vi.mocked(planApi.fetchCVTransformationPlan).mockRejectedValue(
      new planApi.CVTransformationPlanApiError('RESUME_NOT_FOUND', 404, 'Missing')
    );
    render(<TransformationPlanReviewPage />);
    expect(await screen.findByText(/Nie znaleziono planu/)).toBeDefined();
  });

  it('24. renders a network error state', async () => {
    vi.mocked(planApi.fetchCVTransformationPlan).mockRejectedValue(
      new planApi.CVTransformationPlanApiError('NETWORK_OR_CONTRACT_ERROR', 0, 'Offline')
    );
    render(<TransformationPlanReviewPage />);
    expect(await screen.findByText(/Nie udało się połączyć/)).toBeDefined();
  });

  it('25. aborts the active request on cleanup', () => {
    let signal: AbortSignal | undefined;
    vi.mocked(careerApi.fetchResumePositioningContext).mockImplementation((_id, options) => {
      signal = options?.signal;
      return new Promise(() => {});
    });
    const view = render(<TransformationPlanReviewPage />);
    view.unmount();
    expect(signal?.aborted).toBe(true);
  });

  it('26. prevents a double POST', async () => {
    let resolveSave: ((value: CVTransformationPlanApproval) => void) | undefined;
    vi.mocked(approvalApi.saveCVTransformationPlanApproval).mockReturnValue(
      new Promise((resolve) => {
        resolveSave = resolve;
      })
    );
    await renderLoaded();
    completeSelections();
    const submit = screen.getByRole('button', { name: 'Zatwierdź plan' });
    fireEvent.click(submit);
    fireEvent.click(submit);
    expect(approvalApi.saveCVTransformationPlanApproval).toHaveBeenCalledTimes(1);
    resolveSave?.(approval('APPROVED'));
  });

  it('27. forwards the backend fingerprint without calculating another', async () => {
    await renderLoaded();
    fireEvent.click(screen.getByRole('button', { name: 'Zapisz wersję roboczą' }));
    await waitFor(() => expect(approvalApi.saveCVTransformationPlanApproval).toHaveBeenCalled());
    expect(
      vi.mocked(approvalApi.saveCVTransformationPlanApproval).mock.calls[0][2].plan_fingerprint
    ).toBe(plan.plan_fingerprint);
  });

  it('28. sends no action or EvidencePermission in approval body', async () => {
    await renderLoaded();
    fireEvent.click(screen.getByRole('button', { name: 'Zapisz wersję roboczą' }));
    await waitFor(() => expect(approvalApi.saveCVTransformationPlanApproval).toHaveBeenCalled());
    const body = JSON.stringify(
      vi.mocked(approvalApi.saveCVTransformationPlanApproval).mock.calls[0][2]
    );
    expect(body).not.toContain('action');
    expect(body).not.toContain('EvidencePermission');
  });

  it('29. shows SUPERSEDED without treating a freshly loaded plan as stale', async () => {
    vi.mocked(approvalApi.fetchCVTransformationPlanApproval).mockResolvedValue({
      ...approval('SUPERSEDED'),
      plan_fingerprint: 'b'.repeat(64),
    });
    await renderLoaded();
    expect(screen.getByText('SUPERSEDED')).toBeDefined();
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('30. navigates back to Career Positioning', async () => {
    await renderLoaded();
    fireEvent.click(screen.getAllByRole('button', { name: 'Wróć do Career Positioning' })[0]);
    expect(push).toHaveBeenCalledWith('/resumes/resume-example/positioning');
  });

  it('31. does not carry superseded decisions into the current plan', async () => {
    vi.mocked(approvalApi.fetchCVTransformationPlanApproval).mockResolvedValue({
      ...approval('SUPERSEDED'),
      plan_fingerprint: 'b'.repeat(64),
    });
    await renderLoaded();
    expect((screen.getAllByLabelText(/^Akceptuję/)[0] as HTMLInputElement).checked).toBe(false);
  });

  it('32. never calls a CV generator', async () => {
    await renderLoaded();
    expect(approvalApi.saveCVTransformationPlanApproval).not.toHaveBeenCalled();
  });

  it('33. disables all decision controls while an intentional refresh is pending', async () => {
    await renderLoaded();
    let finishRefresh: ((value: { resume_id: string; parent_id: string }) => void) | undefined;
    vi.mocked(careerApi.fetchResumePositioningContext).mockReturnValueOnce(
      new Promise((resolve) => {
        finishRefresh = resolve;
      })
    );

    fireEvent.click(screen.getByRole('button', { name: 'Odśwież plan' }));

    expect(
      (screen.getByRole('button', { name: 'Zapisz wersję roboczą' }) as HTMLButtonElement).disabled
    ).toBe(true);
    expect(
      (screen.getByRole('button', { name: 'Zatwierdź plan' }) as HTMLButtonElement).disabled
    ).toBe(true);
    expect(
      (screen.getAllByRole('radio') as HTMLInputElement[]).every((radio) => radio.disabled)
    ).toBe(true);
    expect((screen.getByRole('checkbox') as HTMLInputElement).disabled).toBe(true);

    finishRefresh?.({ resume_id: 'resume-example', parent_id: 'parent-example' });
    await waitFor(() =>
      expect(
        (screen.getByRole('button', { name: 'Zapisz wersję roboczą' }) as HTMLButtonElement)
          .disabled
      ).toBe(false)
    );
    expect(
      (screen.getAllByRole('radio') as HTMLInputElement[]).every((radio) => !radio.disabled)
    ).toBe(true);
    expect((screen.getByRole('checkbox') as HTMLInputElement).disabled).toBe(false);
  });

  it('34. shows Stage 10C navigation only for APPROVED', async () => {
    vi.mocked(approvalApi.fetchCVTransformationPlanApproval).mockResolvedValue(
      approval('APPROVED')
    );
    await renderLoaded();
    fireEvent.click(screen.getByRole('button', { name: 'Generuj kontrolowany draft' }));
    expect(push).toHaveBeenCalledWith('/resumes/resume-example/positioning/plan/generation');
  });

  it.each(['DRAFT', 'REQUIRES_REVIEW'] as const)(
    '35-36. hides Stage 10C navigation for %s',
    async (status) => {
      vi.mocked(approvalApi.fetchCVTransformationPlanApproval).mockResolvedValue(approval(status));
      await renderLoaded();
      expect(screen.queryByRole('button', { name: 'Generuj kontrolowany draft' })).toBeNull();
    }
  );

  it('37. hides Stage 10C navigation for SUPERSEDED', async () => {
    vi.mocked(approvalApi.fetchCVTransformationPlanApproval).mockResolvedValue({
      ...approval('SUPERSEDED'),
      plan_fingerprint: 'b'.repeat(64),
    });
    await renderLoaded();
    expect(screen.queryByRole('button', { name: 'Generuj kontrolowany draft' })).toBeNull();
  });
});
