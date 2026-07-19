import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import CareerPositioningPage from '@/app/(default)/resumes/[id]/positioning/page';
import * as cpApi from '@/lib/api/career-positioning';
import { CareerPositioningResponse } from '@/lib/types/career-positioning';
import { CPErrorKind, CareerPositioningApiError } from '@/lib/api/career-positioning';

const mockPush = vi.fn();
vi.mock('next/navigation', () => ({
  useRouter: () => ({ push: mockPush }),
  useParams: () => ({ id: 'resume-123' }),
}));

const mockT = (key: string, params?: Record<string, string | number>) => {
  if (params && 'source' in params) {
    return `${key}:${params.source}`;
  }
  return key;
};

vi.mock('@/lib/i18n', () => ({
  useTranslations: () => ({
    t: mockT,
  }),
}));

vi.mock('@/lib/api/career-positioning', () => {
  class MockCareerPositioningApiError extends Error {
    public kind: CPErrorKind;
    public status: number;
    constructor(kind: CPErrorKind, message: string, status = 500) {
      super(message);
      this.kind = kind;
      this.status = status;
    }
  }

  return {
    fetchCareerPositioning: vi.fn(),
    fetchResumeJobContext: vi.fn(),
    fetchResumePositioningContext: vi.fn(),
    CareerPositioningApiError: MockCareerPositioningApiError,
  };
});

const mockValidData: CareerPositioningResponse = {
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
    overqualification: {
      availability: 'AVAILABLE',
      level: 'NONE',
      rationale: ['No risk of overqualification'],
    },
    underqualification: {
      availability: 'AVAILABLE',
      level: 'NONE',
      rationale: ['No risk of underqualification'],
    },
    stability_concern: { availability: 'UNSUPPORTED', level: null, rationale: [] },
    sector_gap: { availability: 'AVAILABLE', level: 'NONE', rationale: ['Matched sector'] },
    functional_gap: { availability: 'AVAILABLE', level: 'LOW', rationale: ['Minor gap'] },
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
    recommendation: 'Excellent match recommendation details.',
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

const mockResumeResponseData = {
  resume_id: 'resume-123',
  parent_id: 'parent-123',
};

describe('CareerPositioningPage Component', () => {
  let consoleSpy: ReturnType<typeof vi.spyOn>;

  beforeEach(() => {
    vi.clearAllMocks();
    consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
  });

  afterEach(() => {
    consoleSpy.mockRestore();
  });

  it('1. renders loading skeleton on mount', () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockReturnValue(new Promise(() => {}));
    render(<CareerPositioningPage />);
    const skeleton = screen.getByRole('generic', { busy: true });
    expect(skeleton).toBeDefined();
  });

  it('2. has screen reader loading label', () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockReturnValue(new Promise(() => {}));
    render(<CareerPositioningPage />);
    const skeleton = screen.getByRole('generic', { busy: true });
    expect(skeleton.getAttribute('aria-label')).toBe('careerPositioning.loading');
  });

  it('3. renders page title correctly after load', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.title')).toBeDefined();
    });
  });

  it('4. renders job title correctly after load', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('Regional Manager')).toBeDefined();
    });
  });

  it('5. renders fit score percentage correctly after load', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('95%')).toBeDefined();
    });
  });

  it('6. applies STRONG fit level style', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.fitLevel.STRONG')).toBeDefined();
    });
  });

  it('7. applies GOOD fit level style', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue({
      ...mockValidData,
      positioning: { ...mockValidData.positioning, fit_level: 'GOOD' },
    });

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.fitLevel.GOOD')).toBeDefined();
    });
  });

  it('8. applies MODERATE fit level style', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue({
      ...mockValidData,
      positioning: { ...mockValidData.positioning, fit_level: 'MODERATE' },
    });

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.fitLevel.MODERATE')).toBeDefined();
    });
  });

  it('9. applies LOW fit level style', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue({
      ...mockValidData,
      positioning: { ...mockValidData.positioning, fit_level: 'LOW' },
    });

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.fitLevel.LOW')).toBeDefined();
    });
  });

  it('10. renders job matrix header', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.jobMatrix')).toBeDefined();
    });
  });

  it('11. renders detected job level correctly in matrix', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getAllByText('careerPositioning.level.MANAGER').length).toBeGreaterThan(0);
    });
  });

  it('12. renders candidate profile level correctly in matrix', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      const candidatesText = screen.getAllByText('careerPositioning.level.MANAGER');
      expect(candidatesText.length).toBeGreaterThan(0);
    });
  });

  it('13. renders positioning strategy strategy details', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.strategyLabels.MANAGER_LEADERSHIP')).toBeDefined();
    });
  });

  it('14. renders overqualification risk details correctly', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.overqualification')).toBeDefined();
      expect(screen.getAllByText('careerPositioning.risk.NONE').length).toBeGreaterThan(0);
    });
  });

  it('15. renders underqualification risk details correctly', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.underqualification')).toBeDefined();
    });
  });

  it('16. renders stability concern risk status correctly', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.stabilityConcern')).toBeDefined();
      expect(screen.getByText('careerPositioning.unsupportedRisk')).toBeDefined();
    });
  });

  it('17. renders sector gap risk details correctly', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.sectorGap')).toBeDefined();
    });
  });

  it('18. renders functional gap risk details correctly', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.functionalGap')).toBeDefined();
      expect(screen.getByText('careerPositioning.risk.LOW')).toBeDefined();
    });
  });

  it('19. renders risk rationales texts', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('No risk of overqualification')).toBeDefined();
      expect(screen.getByText('No risk of underqualification')).toBeDefined();
      expect(screen.getByText('Minor gap')).toBeDefined();
    });
  });

  it('20. renders overall recommendation text in overview', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('Excellent match recommendation details.')).toBeDefined();
    });
  });

  it('21. displays correct used library entries counter', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('5')).toBeDefined();
    });
  });

  it('22. displays correct direct matches counter', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getAllByText('1').length).toBeGreaterThan(0);
    });
  });

  it('23. displays correct transferable matches counter', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getAllByText('1').length).toBeGreaterThan(0);
    });
  });

  it('24. displays correct missing critical matches counter', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('0')).toBeDefined();
    });
  });

  it('25. switches tabs to competencies section', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      const tabButton = screen.getByRole('button', { name: 'careerPositioning.tabCompetencies' });
      fireEvent.click(tabButton);
    });

    expect(screen.getByText('team leading')).toBeDefined();
  });

  it('26. displays direct competency source mapping label', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      const tabButton = screen.getByRole('button', { name: 'careerPositioning.tabCompetencies' });
      fireEvent.click(tabButton);
    });

    expect(
      screen.getAllByText('careerPositioning.sourceLabel:Previous Job').length
    ).toBeGreaterThan(0);
  });

  it('27. displays transferable competency name', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      const tabButton = screen.getByRole('button', { name: 'careerPositioning.tabCompetencies' });
      fireEvent.click(tabButton);
    });

    expect(screen.getByText('budgeting')).toBeDefined();
  });

  it('28. switches tabs to strategy section', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      const tabButton = screen.getByRole('button', { name: 'careerPositioning.tabStrategy' });
      fireEvent.click(tabButton);
    });

    expect(
      screen.getByText('careerPositioning.variantLabel: careerPositioning.manager')
    ).toBeDefined();
  });

  it('29. renders recommended variant fields in strategy tab', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      const tabButton = screen.getByRole('button', { name: 'careerPositioning.tabStrategy' });
      fireEvent.click(tabButton);
    });

    expect(screen.getByText('Title Mgr')).toBeDefined();
    expect(screen.getByText('Summary Mgr')).toBeDefined();
  });

  it('30. switches to subtab of unsupported narrative variant', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      const tabButton = screen.getByRole('button', { name: 'careerPositioning.tabStrategy' });
      fireEvent.click(tabButton);
    });

    const dirSubtab = screen.getByRole('button', { name: 'careerPositioning.director' });
    fireEvent.click(dirSubtab);

    expect(screen.getByText('careerPositioning.unsupportedVariant')).toBeDefined();
  });

  it('31. shows unsupported variant block details reasons', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      const tabButton = screen.getByRole('button', { name: 'careerPositioning.tabStrategy' });
      fireEvent.click(tabButton);
    });

    const dirSubtab = screen.getByRole('button', { name: 'careerPositioning.director' });
    fireEvent.click(dirSubtab);

    expect(screen.getByText('No director scale')).toBeDefined();
  });

  it('32. handles RESUME_NOT_FOUND error kind state', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockRejectedValue(
      new CareerPositioningApiError('RESUME_NOT_FOUND', 'Resume not found', 404)
    );

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.errorResumeNotFound')).toBeDefined();
    });
  });

  it('33. handles RESUME_NOT_TAILORED error kind state', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockRejectedValue(
      new CareerPositioningApiError('RESUME_NOT_TAILORED', 'Resume not tailored', 400)
    );

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.errorResumeNotTailored')).toBeDefined();
    });
  });

  it('34. handles JOB_CONTEXT_NOT_FOUND error kind state', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockRejectedValue(
      new CareerPositioningApiError('JOB_CONTEXT_NOT_FOUND', 'Context not found', 404)
    );

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.errorJobContextNotFound')).toBeDefined();
    });
  });

  it('35. handles JOB_NOT_FOUND error kind state', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockRejectedValue(
      new CareerPositioningApiError('JOB_NOT_FOUND', 'Job not found', 404)
    );

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.errorJobNotFound')).toBeDefined();
    });
  });

  it('36. handles insufficient data gracefully without showing error page', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue({
      ...mockValidData,
      limitations: { insufficient_data: true, messages: ['Empty lib'] },
    });

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.limitations')).toBeDefined();
      expect(screen.queryByText('careerPositioning.errorNoTruthLibrary')).toBeNull();
    });
  });

  it('37. handles INVALID_CONTRACT error kind state', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockRejectedValue(
      new CareerPositioningApiError('INVALID_CONTRACT', 'Invalid schema version', 400)
    );

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.errorInvalidContract')).toBeDefined();
    });
  });

  it('38. handles NETWORK_ERROR error kind state', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockRejectedValue(
      new CareerPositioningApiError('NETWORK_ERROR', 'Network communication failed.', 500)
    );

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.errorNetwork')).toBeDefined();
    });
  });

  it('39. handles UNKNOWN error kind state', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockRejectedValue(
      new CareerPositioningApiError('UNKNOWN', 'System crashed', 500)
    );

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.errorUnknown')).toBeDefined();
    });
  });

  it('40. handles refresh in-place behavior correctly', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.refresh')).toBeDefined();
    });

    const refreshBtn = screen.getByRole('button', { name: 'careerPositioning.refresh' });
    fireEvent.click(refreshBtn);

    // Skeleton should NOT render because refreshing is true and loading is false
    expect(screen.queryByLabelText('careerPositioning.loading')).toBeNull();
    // Regional Manager should still be on screen
    expect(screen.getByText('Regional Manager')).toBeDefined();
  });

  it('41. backToResume button navigates back', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      const backBtn = screen.getByRole('button', { name: 'careerPositioning.backToResume' });
      fireEvent.click(backBtn);
    });

    expect(mockPush).toHaveBeenCalledWith('/resumes/resume-123');
  });

  it('42. counts fetch calls and passes AbortSignal', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('Regional Manager')).toBeDefined();
    });

    expect(cpApi.fetchResumePositioningContext).toHaveBeenCalled();
    expect(cpApi.fetchResumeJobContext).toHaveBeenCalled();
    expect(cpApi.fetchCareerPositioning).toHaveBeenCalled();

    const calls = vi.mocked(cpApi.fetchCareerPositioning).mock.calls;
    const signalArg = calls[calls.length - 1][1]?.signal;
    expect(signalArg).toBeInstanceOf(AbortSignal);
  });

  it('43. cleans up and aborts previous signals on new loads/unmounts', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    const { unmount } = render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('Regional Manager')).toBeDefined();
    });

    const calls = vi.mocked(cpApi.fetchCareerPositioning).mock.calls;
    const signalArg = calls[calls.length - 1][1]?.signal;
    expect(signalArg?.aborted).toBe(false);

    unmount();

    expect(signalArg?.aborted).toBe(true);
  });

  it('44. displays confidence percentage and manual review flag', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue({
      ...mockValidData,
      positioning: {
        ...mockValidData.positioning,
        confidence: 0.88,
        requires_human_review: true,
      },
    });

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.confidenceLabel: 88%')).toBeDefined();
      expect(screen.getByText('careerPositioning.humanReviewTitle')).toBeDefined();
      expect(screen.getByText('careerPositioning.humanReviewDescription')).toBeDefined();
    });
  });

  it('45. displays refreshError alert on failed refresh attempts', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);

    await waitFor(() => {
      expect(screen.getByText('Regional Manager')).toBeDefined();
    });

    vi.mocked(cpApi.fetchCareerPositioning).mockRejectedValue(new Error('Refresh failed'));

    const refreshBtn = screen.getByRole('button', { name: 'careerPositioning.refresh' });
    fireEvent.click(refreshBtn);

    await waitFor(() => {
      expect(screen.getByText('careerPositioning.refreshError')).toBeDefined();
    });
  });

  it('46. navigates to the transformation plan review', async () => {
    vi.mocked(cpApi.fetchResumePositioningContext).mockResolvedValue(mockResumeResponseData);
    vi.mocked(cpApi.fetchResumeJobContext).mockResolvedValue({ job_id: 'job-123' });
    vi.mocked(cpApi.fetchCareerPositioning).mockResolvedValue(mockValidData);

    render(<CareerPositioningPage />);
    await screen.findByText('Regional Manager');
    fireEvent.click(screen.getByRole('button', { name: 'Przejrzyj plan CV' }));
    expect(mockPush).toHaveBeenCalledWith('/resumes/resume-123/positioning/plan');
  });
});
