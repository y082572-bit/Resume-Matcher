import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import CareerPositioningPage from '@/app/(default)/resumes/[id]/positioning/page';
import ResumeViewerPage from '@/app/(default)/resumes/[id]/page';
import * as cpApi from '@/lib/api/career-positioning';
import * as resumeApi from '@/lib/api/resume';
import type { CareerPositioningResponse } from '@/lib/types/career-positioning';

const RESUME_ID = 'resume-e2e-123';
const JOB_ID = 'job-e2e-456';
const JOB_TITLE = 'Anonymized Operations Manager';
const mockPush = vi.fn();
const decrementResumes = vi.fn();
const setHasMasterResume = vi.fn();

vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPush }),
  useParams: () => ({ id: RESUME_ID }),
}));

const translate = (key: string, params?: Record<string, string | number>) => {
  if (params && 'source' in params) {
    return `${key}:${params.source}`;
  }
  return key;
};

vi.mock('@/lib/i18n', () => ({
  useTranslations: () => ({ t: translate }),
}));

vi.mock('@/lib/api/career-positioning', () => {
  class MockCareerPositioningApiError extends Error {
    public kind: string;
    public status: number;

    constructor(kind: string, message: string, status = 500) {
      super(message);
      this.kind = kind;
      this.status = status;
    }
  }

  return {
    fetchResumePositioningContext: vi.fn(),
    fetchResumeJobContext: vi.fn(),
    fetchCareerPositioning: vi.fn(),
    CareerPositioningApiError: MockCareerPositioningApiError,
  };
});

vi.mock('@/lib/api/resume', () => ({
  fetchResume: vi.fn(),
  deleteResume: vi.fn(),
  retryProcessing: vi.fn(),
  renameResume: vi.fn(),
  downloadResumePdf: vi.fn(),
  getResumePdfUrl: vi.fn(),
}));

vi.mock('@/lib/context/status-cache', () => ({
  useStatusCache: () => ({ decrementResumes, setHasMasterResume }),
}));

vi.mock('@/lib/context/language-context', () => ({
  useLanguage: () => ({ uiLanguage: 'en' }),
}));

vi.mock('@/components/enrichment/enrichment-modal', () => ({ EnrichmentModal: () => null }));
vi.mock('@/components/dashboard/resume-component', () => ({ default: () => null }));

const report: CareerPositioningResponse = {
  schema_version: '1.0',
  generated_at: '2026-07-18T08:00:00Z',
  job_id: JOB_ID,
  job_title: JOB_TITLE,
  positioning: {
    detected_job_level: 'MANAGER',
    candidate_profile_level: 'EXPERT',
    level_gap: -1,
    positioning_strategy: 'CONTROLLED_ELEVATION',
    title_treatment: 'KEEP',
    fit_level: 'GOOD',
    fit_score: 91,
    confidence: 0.87,
    requires_human_review: false,
  },
  risks: {
    overqualification: {
      availability: 'AVAILABLE',
      level: 'NONE',
      rationale: ['Backend overqualification rationale'],
    },
    underqualification: {
      availability: 'AVAILABLE',
      level: 'LOW',
      rationale: ['Backend underqualification rationale'],
    },
    stability_concern: { availability: 'UNSUPPORTED', level: null, rationale: [] },
    sector_gap: { availability: 'AVAILABLE', level: 'NONE', rationale: [] },
    functional_gap: { availability: 'AVAILABLE', level: 'LOW', rationale: [] },
  },
  competencies: {
    direct_matches: [
      {
        name: 'Process analysis',
        reason: 'Backend direct reason',
        source: 'Anonymized experience',
      },
    ],
    transferable_matches: [
      {
        name: 'Partner management',
        reason: 'Backend transferable reason',
        source: 'Approved mapping',
      },
    ],
    missing_critical: [
      {
        name: 'Forecasting',
        reason: 'Backend missing reason',
        source: 'Role requirements',
      },
    ],
    excessive_for_role: [],
  },
  strategy: {
    recommendation: 'BACKEND_RECOMMENDATION_MARKER',
    recommended_narrative: 'MANAGER',
    elements_to_emphasize: ['BACKEND_EMPHASIS_MARKER'],
    elements_to_flatten: [],
    elements_to_remove_or_deprioritize: [],
    title_positioning: 'BACKEND_TITLE_MARKER',
    summary_positioning: 'BACKEND_SUMMARY_MARKER',
    narrative_variants: [
      {
        type: 'EXPERT',
        availability: 'AVAILABLE',
        recommended: false,
        title_positioning: 'Backend expert title',
        summary_positioning: 'Backend expert summary',
        elements_to_emphasize: ['Backend expert evidence'],
        elements_to_flatten: [],
        elements_to_remove_or_deprioritize: [],
        risk_level: 'LOW',
        limitations: [],
      },
      {
        type: 'MANAGER',
        availability: 'AVAILABLE',
        recommended: true,
        title_positioning: 'BACKEND_TITLE_MARKER',
        summary_positioning: 'BACKEND_SUMMARY_MARKER',
        elements_to_emphasize: ['BACKEND_EMPHASIS_MARKER'],
        elements_to_flatten: [],
        elements_to_remove_or_deprioritize: [],
        risk_level: 'LOW',
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
        limitations: ['BACKEND_UNSUPPORTED_MARKER'],
      },
    ],
  },
  evidence: {
    used_truth_items_count: 4,
    direct_evidence_count: 1,
    transferable_evidence_count: 1,
    unresolved_items_count: 1,
  },
  limitations: { insufficient_data: false, messages: [] },
};

function resolveCareerFlow(payload: CareerPositioningResponse = report) {
  vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue({
    resume_id: RESUME_ID,
    parent_id: 'master-resume-e2e',
  });
  vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: JOB_ID });
  vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(payload);
}

async function renderLoadedCareerPage(payload: CareerPositioningResponse = report) {
  resolveCareerFlow(payload);
  const rendered = render(<CareerPositioningPage />);
  await screen.findByText(JOB_TITLE);
  return rendered;
}

async function openSection(name: string) {
  fireEvent.click(await screen.findByRole('button', { name }));
}

describe('Career Positioning Stage 8 integration flow', () => {
  let consoleError: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.clearAllMocks();
    localStorage.clear();
    consoleError = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    vi.mocked(resumeApi.fetchResume).mockResolvedValue({
      resume_id: RESUME_ID,
      raw_resume: {
        id: null,
        content: '{}',
        content_type: 'json',
        created_at: '2026-07-18T08:00:00Z',
        processing_status: 'ready',
      },
      processed_resume: {
        personalInfo: { name: 'Test Candidate', title: 'Operations Specialist' },
        summary: 'Anonymized profile',
        workExperience: [],
        education: [],
        personalProjects: [],
        additional: {
          technicalSkills: [],
          languages: [],
          certificationsTraining: [],
          awards: [],
        },
      },
      parent_id: 'master-resume-e2e',
      title: 'Anonymized tailored resume',
    });
  });

  afterEach(() => {
    consoleError.mockRestore();
  });

  it('1. renders an accessible skeleton while the first request is pending', () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockReturnValue(new Promise(() => undefined));

    render(<CareerPositioningPage />);

    const skeleton = screen.getByRole('generic', { busy: true });
    expect(skeleton.getAttribute('aria-label')).toBe('careerPositioning.loading');
  });

  it('2. enters Career Positioning from the real resume view using the correct URL', async () => {
    render(<ResumeViewerPage />);
    fireEvent.click(
      await screen.findByRole('button', { name: 'careerPositioning.positioningNavLabel' })
    );

    expect(mockPush).toHaveBeenCalledWith(`/resumes/${RESUME_ID}/positioning`);
  });

  it('3. requests resume context, job context, and the report with correct identifiers', async () => {
    await renderLoadedCareerPage();

    expect(vi.mocked(cpApi.fetchResumePositioningContext).mock.calls[0][0]).toBe(RESUME_ID);
    expect(vi.mocked(cpApi.fetchResumeJobContext).mock.calls[0][0]).toBe(RESUME_ID);
    expect(vi.mocked(cpApi.fetchCareerPositioning).mock.calls[0][0]).toBe(JOB_ID);
  });

  it('4. sends exactly one initial request set in the required sequence', async () => {
    await renderLoadedCareerPage();

    expect(cpApi.fetchResumePositioningContext).toHaveBeenCalledTimes(1);
    expect(cpApi.fetchResumeJobContext).toHaveBeenCalledTimes(1);
    expect(cpApi.fetchCareerPositioning).toHaveBeenCalledTimes(1);
    const resumeOrder = vi.mocked(cpApi.fetchResumePositioningContext).mock.invocationCallOrder[0];
    const jobOrder = vi.mocked(cpApi.fetchResumeJobContext).mock.invocationCallOrder[0];
    const reportOrder = vi.mocked(cpApi.fetchCareerPositioning).mock.invocationCallOrder[0];
    expect(resumeOrder).toBeLessThan(jobOrder);
    expect(jobOrder).toBeLessThan(reportOrder);
  });

  it('5. renders the offer name returned by the backend', async () => {
    await renderLoadedCareerPage();

    expect(screen.getByText(JOB_TITLE)).toBeDefined();
  });

  it('6. renders translated job level, candidate level, and strategy', async () => {
    await renderLoadedCareerPage();

    expect(screen.getByText('careerPositioning.level.MANAGER')).toBeDefined();
    expect(screen.getByText('careerPositioning.level.EXPERT')).toBeDefined();
    expect(screen.getByText('careerPositioning.strategyLabels.CONTROLLED_ELEVATION')).toBeDefined();
  });

  it('7. renders the backend seniority score and fit level without recalculation', async () => {
    await renderLoadedCareerPage();

    expect(screen.getByText('91%')).toBeDefined();
    expect(screen.getByText('careerPositioning.fitLevel.GOOD')).toBeDefined();
  });

  it('8. renders backend confidence as a percentage', async () => {
    await renderLoadedCareerPage();

    expect(screen.getByText('careerPositioning.confidenceLabel: 87%')).toBeDefined();
  });

  it('9. renders the human-review flag when the backend enables it', async () => {
    await renderLoadedCareerPage({
      ...report,
      positioning: { ...report.positioning, requires_human_review: true },
    });

    expect(screen.getByText('careerPositioning.humanReviewTitle')).toBeDefined();
  });

  it('10. renders overqualification, underqualification, and unsupported risk states', async () => {
    await renderLoadedCareerPage();

    expect(screen.getByText('careerPositioning.overqualification')).toBeDefined();
    expect(screen.getByText('careerPositioning.underqualification')).toBeDefined();
    expect(screen.getAllByText('careerPositioning.unsupportedRisk').length).toBeGreaterThan(0);
    expect(screen.getByText('Backend underqualification rationale')).toBeDefined();
  });

  it('11. renders direct matches on the competencies tab', async () => {
    await renderLoadedCareerPage();
    await openSection('careerPositioning.tabCompetencies');

    expect(screen.getByText('Process analysis')).toBeDefined();
    expect(screen.getByText('Backend direct reason')).toBeDefined();
  });

  it('12. renders transferable matches on the competencies tab', async () => {
    await renderLoadedCareerPage();
    await openSection('careerPositioning.tabCompetencies');

    expect(screen.getByText('Partner management')).toBeDefined();
    expect(screen.getByText('Backend transferable reason')).toBeDefined();
  });

  it('13. renders missing critical competencies on the competencies tab', async () => {
    await renderLoadedCareerPage();
    await openSection('careerPositioning.tabCompetencies');

    expect(screen.getByText('Forecasting')).toBeDefined();
    expect(screen.getByText('Backend missing reason')).toBeDefined();
  });

  it('14. renders the recommended narrative exactly from the backend', async () => {
    await renderLoadedCareerPage();
    await openSection('careerPositioning.tabStrategy');

    expect(screen.getByText('BACKEND_TITLE_MARKER')).toBeDefined();
    expect(screen.getByText('BACKEND_SUMMARY_MARKER')).toBeDefined();
    expect(screen.getByText('BACKEND_EMPHASIS_MARKER')).toBeDefined();
    expect(screen.getByText('careerPositioning.recommended')).toBeDefined();
  });

  it('15. renders an unsupported narrative and its backend limitation', async () => {
    await renderLoadedCareerPage();
    await openSection('careerPositioning.tabStrategy');
    fireEvent.click(screen.getByRole('button', { name: 'careerPositioning.director' }));

    expect(screen.getByText('careerPositioning.unsupportedVariant')).toBeDefined();
    expect(screen.getByText('BACKEND_UNSUPPORTED_MARKER')).toBeDefined();
  });

  it('16. refreshes all three contexts and replaces the report', async () => {
    await renderLoadedCareerPage();
    const refreshedTitle = 'Refreshed anonymized offer';
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue({
      ...report,
      job_title: refreshedTitle,
    });

    fireEvent.click(screen.getByRole('button', { name: 'careerPositioning.refresh' }));

    expect(await screen.findByText(refreshedTitle)).toBeDefined();
    expect(cpApi.fetchResumePositioningContext).toHaveBeenCalledTimes(2);
    expect(cpApi.fetchResumeJobContext).toHaveBeenCalledTimes(2);
    expect(cpApi.fetchCareerPositioning).toHaveBeenCalledTimes(2);
  });

  it('17. keeps the prior report visible when refresh fails', async () => {
    await renderLoadedCareerPage();
    vi.mocked(cpApi.fetchCareerPositioning).mockRejectedValue(new Error('Refresh failed'));

    fireEvent.click(screen.getByRole('button', { name: 'careerPositioning.refresh' }));

    expect(await screen.findByText('careerPositioning.refreshError')).toBeDefined();
    expect(screen.getByText(JOB_TITLE)).toBeDefined();
    expect(screen.getByText('91%')).toBeDefined();
  });

  it('18. passes one shared AbortSignal to every request in a load', async () => {
    await renderLoadedCareerPage();

    const resumeSignal = vi.mocked(cpApi.fetchResumePositioningContext).mock.calls[0][1]?.signal;
    const jobSignal = vi.mocked(cpApi.fetchResumeJobContext).mock.calls[0][1]?.signal;
    const reportSignal = vi.mocked(cpApi.fetchCareerPositioning).mock.calls[0][1]?.signal;
    expect(resumeSignal).toBeInstanceOf(AbortSignal);
    expect(jobSignal).toBe(resumeSignal);
    expect(reportSignal).toBe(resumeSignal);
  });

  it('19. aborts the active request during component cleanup', async () => {
    const rendered = await renderLoadedCareerPage();
    const signal = vi.mocked(cpApi.fetchCareerPositioning).mock.calls[0][1]?.signal;
    expect(signal?.aborted).toBe(false);

    rendered.unmount();

    expect(signal?.aborted).toBe(true);
  });

  it('20. does not expose raw enum values as standalone interface text', async () => {
    await renderLoadedCareerPage();

    expect(screen.queryByText(/^MANAGER$/)).toBeNull();
    expect(screen.queryByText(/^EXPERT$/)).toBeNull();
    expect(screen.queryByText(/^CONTROLLED_ELEVATION$/)).toBeNull();
  });

  it('21. does not render private fixture values', async () => {
    await renderLoadedCareerPage();

    expect(document.body.textContent).not.toContain('PRIVATE_CONTACT_VALUE');
    expect(document.body.textContent).not.toContain('PRIVATE_ADDRESS_VALUE');
  });

  it('22. displays backend narrative markers and does not synthesize alternatives', async () => {
    await renderLoadedCareerPage();
    await openSection('careerPositioning.tabStrategy');

    expect(screen.getByText('BACKEND_TITLE_MARKER')).toBeDefined();
    expect(screen.getByText('BACKEND_SUMMARY_MARKER')).toBeDefined();
    expect(document.body.textContent).not.toContain('FRONTEND_GENERATED_NARRATIVE');
  });

  it('23. does not add finance, board, or people-management claims absent from backend data', async () => {
    await renderLoadedCareerPage();
    await openSection('careerPositioning.tabStrategy');

    expect(document.body.textContent).not.toMatch(/P&L|Board|budget|people management/i);
  });

  it('24. navigates back to the source resume', async () => {
    await renderLoadedCareerPage();

    fireEvent.click(screen.getByRole('button', { name: 'careerPositioning.backToResume' }));

    expect(mockPush).toHaveBeenCalledWith(`/resumes/${RESUME_ID}`);
  });
});
