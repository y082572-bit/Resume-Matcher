'use client';

import React, { useEffect, useState, useRef } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { Button } from '@/components/ui/button';
import { RetroTabs } from '@/components/ui/retro-tabs';
import {
  fetchCareerPositioning,
  fetchResumeJobContext,
  fetchResumePositioningContext,
  CareerPositioningApiError,
  CPErrorKind,
} from '@/lib/api/career-positioning';
import {
  CareerPositioningResponse,
  FitLevel,
  RoleLevel,
  RiskLevel,
  PositioningStrategy,
} from '@/lib/types/career-positioning';
import { useTranslations } from '@/lib/i18n';
import {
  ArrowLeft,
  AlertTriangle,
  ShieldAlert,
  CheckCircle2,
  TrendingUp,
  Compass,
  Sparkles,
  Info,
  RefreshCw,
  ClipboardCheck,
} from 'lucide-react';

const FIT_LEVEL_STYLES: Record<FitLevel, { color: string; bg: string; text: string }> = {
  LOW: { color: 'text-red-600 border-red-600', bg: 'bg-red-50', text: 'text-red-700' },
  MODERATE: {
    color: 'text-orange-600 border-orange-600',
    bg: 'bg-orange-50',
    text: 'text-orange-700',
  },
  GOOD: { color: 'text-blue-600 border-blue-600', bg: 'bg-blue-50', text: 'text-blue-700' },
  STRONG: { color: 'text-green-600 border-green-600', bg: 'bg-green-50', text: 'text-green-700' },
};

const LEVEL_COLORS: Record<RoleLevel, string> = {
  UNKNOWN: 'bg-gray-100 text-gray-800 border-gray-300',
  JUNIOR: 'bg-green-100 text-green-800 border-green-300',
  SPECIALIST: 'bg-blue-100 text-blue-800 border-blue-300',
  EXPERT: 'bg-indigo-100 text-indigo-800 border-indigo-300',
  MANAGER: 'bg-purple-100 text-purple-800 border-purple-300',
  DIRECTOR: 'bg-pink-100 text-pink-800 border-pink-300',
  EXECUTIVE: 'bg-red-100 text-red-800 border-red-300',
};

const RISK_COLORS: Record<RiskLevel, string> = {
  NONE: 'bg-green-50 text-green-700 border-green-200',
  LOW: 'bg-blue-50 text-blue-700 border-blue-200',
  MEDIUM: 'bg-amber-50 text-amber-700 border-amber-300',
  HIGH: 'bg-red-50 text-red-700 border-red-300',
};

export default function CareerPositioningPage() {
  const { t } = useTranslations();
  const params = useParams();
  const router = useRouter();

  const resumeId = typeof params?.id === 'string' ? params.id : '';

  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [errorType, setErrorType] = useState<CPErrorKind | null>(null);
  const [refreshError, setRefreshError] = useState<string | null>(null);

  const [jobTitle, setJobTitle] = useState('');
  const [positioningData, setPositioningData] = useState<CareerPositioningResponse | null>(null);

  const [activeSection, setActiveSection] = useState('overview');
  const [selectedNarrative, setSelectedNarrative] = useState<'EXPERT' | 'MANAGER' | 'DIRECTOR'>(
    'EXPERT'
  );

  const activeControllerRef = useRef<AbortController | null>(null);

  const translateLevel = (lvl: RoleLevel): string => {
    return t(`careerPositioning.level.${lvl}`) || t('careerPositioning.unknownValue');
  };

  const translateStrategy = (strat: PositioningStrategy): string => {
    return t(`careerPositioning.strategyLabels.${strat}`) || t('careerPositioning.unknownValue');
  };

  const translateFitLevel = (fit: FitLevel): string => {
    return t(`careerPositioning.fitLevel.${fit}`) || t('careerPositioning.unknownValue');
  };

  const translateRiskLevel = (risk: RiskLevel | null): string => {
    if (!risk) return t('careerPositioning.unknownValue');
    return t(`careerPositioning.risk.${risk}`) || t('careerPositioning.unknownValue');
  };

  const loadData = React.useCallback(
    async (isRetry = false, isRefresh = false, signal: AbortSignal) => {
      if (!resumeId) return;

      if (isRefresh) {
        setRefreshing(true);
        setRefreshError(null);
      } else {
        setLoading(true);
        if (!isRetry) {
          setErrorType(null);
        }
      }

      try {
        await fetchResumePositioningContext(resumeId, { signal });
        const jobContext = await fetchResumeJobContext(resumeId, { signal });
        const positioning = await fetchCareerPositioning(jobContext.job_id, { signal });

        setPositioningData(positioning);
        setJobTitle(positioning.job_title);
        setErrorType(null);
        setRefreshError(null);

        if (positioning.strategy?.recommended_narrative) {
          setSelectedNarrative(positioning.strategy.recommended_narrative);
        }
      } catch (err: unknown) {
        if (signal.aborted) {
          return;
        }
        console.error('Failed to load career positioning:', err);
        if (isRefresh) {
          setRefreshError(t('careerPositioning.refreshError'));
        } else {
          if (err instanceof CareerPositioningApiError) {
            setErrorType(err.kind);
          } else {
            setErrorType('UNKNOWN');
          }
        }
      } finally {
        setLoading(false);
        setRefreshing(false);
      }
    },
    [resumeId, t]
  );

  const triggerLoad = React.useCallback(
    (isRetry = false, isRefresh = false) => {
      if (activeControllerRef.current) {
        activeControllerRef.current.abort();
      }
      const controller = new AbortController();
      activeControllerRef.current = controller;
      loadData(isRetry, isRefresh, controller.signal);
    },
    [loadData]
  );

  useEffect(() => {
    triggerLoad();
    return () => {
      if (activeControllerRef.current) {
        activeControllerRef.current.abort();
      }
    };
  }, [resumeId, triggerLoad]);

  const showSkeleton = loading && !positioningData;
  if (showSkeleton) {
    return (
      <div
        className="min-h-screen bg-[#F6F5EE] py-12 px-4 md:px-8"
        aria-busy="true"
        aria-label={t('careerPositioning.loading')}
      >
        <div className="max-w-7xl mx-auto space-y-8 animate-pulse">
          <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4 border-2 border-black bg-white p-6 md:p-8 shadow-[4px_4px_0px_0px_rgba(0,0,0,1)]">
            <div className="space-y-4 w-full md:w-2/3">
              <div className="h-8 bg-gray-200 border border-black w-24"></div>
              <div className="h-10 bg-gray-200 border border-black w-3/4"></div>
              <div className="h-6 bg-gray-200 border border-black w-1/2"></div>
            </div>
            <div className="h-20 bg-gray-200 border border-black w-full md:w-48 shadow-[2px_2px_0px_0px_rgba(0,0,0,1)]"></div>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-12 gap-8">
            <div className="lg:col-span-4 space-y-8">
              <div className="h-64 bg-white border-2 border-black shadow-[4px_4px_0px_0px_rgba(0,0,0,1)]"></div>
              <div className="h-64 bg-white border-2 border-black shadow-[4px_4px_0px_0px_rgba(0,0,0,1)]"></div>
            </div>
            <div className="lg:col-span-8 space-y-8">
              <div className="h-12 bg-gray-200 border-2 border-black shadow-[2px_2px_0px_0px_rgba(0,0,0,1)] w-full"></div>
              <div className="h-96 bg-white border-2 border-black shadow-[4px_4px_0px_0px_rgba(0,0,0,1)]"></div>
            </div>
          </div>
        </div>
      </div>
    );
  }

  if (errorType) {
    const errorMessages: Record<CPErrorKind, string> = {
      RESUME_NOT_FOUND: t('careerPositioning.errorResumeNotFound'),
      RESUME_NOT_TAILORED: t('careerPositioning.errorResumeNotTailored'),
      JOB_CONTEXT_NOT_FOUND: t('careerPositioning.errorJobContextNotFound'),
      JOB_NOT_FOUND: t('careerPositioning.errorJobNotFound'),
      NO_TRUTH_LIBRARY: t('careerPositioning.errorNoTruthLibrary'),
      INVALID_TRUTH_LIBRARY: t('careerPositioning.errorInvalidTruthLibrary'),
      TRUTH_LIBRARY_READ_ERROR: t('careerPositioning.errorTruthLibraryReadError'),
      INVALID_CONTRACT: t('careerPositioning.errorInvalidContract'),
      NETWORK_ERROR: t('careerPositioning.errorNetwork'),
      UNKNOWN: t('careerPositioning.errorUnknown'),
    };

    return (
      <div className="min-h-screen flex flex-col items-center justify-center bg-[#F6F5EE] p-4">
        <div className="border-2 border-black bg-white p-8 text-center max-w-md shadow-[4px_4px_0px_0px_rgba(0,0,0,1)]">
          <ShieldAlert className="w-12 h-12 text-red-600 mx-auto mb-4" />
          <h1 className="font-serif text-2xl font-bold uppercase mb-2">{t('common.error')}</h1>
          <p className="font-mono text-xs text-red-700 mb-6 leading-relaxed">
            {errorMessages[errorType] || t('careerPositioning.errorMessage')}
          </p>
          <div className="flex gap-2">
            <Button
              variant="outline"
              onClick={() => router.push(`/resumes/${resumeId}`)}
              className="flex-1"
            >
              {t('careerPositioning.backToResume')}
            </Button>
            <Button onClick={() => triggerLoad(true, false)} className="flex-1">
              {t('careerPositioning.retry')}
            </Button>
          </div>
        </div>
      </div>
    );
  }

  if (!positioningData) {
    return (
      <div className="min-h-screen flex flex-col items-center justify-center bg-[#F6F5EE] p-4">
        <div className="border-2 border-black bg-white p-8 text-center max-w-md shadow-[4px_4px_0px_0px_rgba(0,0,0,1)]">
          <AlertTriangle className="w-12 h-12 text-amber-500 mx-auto mb-4" />
          <p className="font-mono text-xs text-steel-grey mb-6">
            {t('careerPositioning.emptyState')}
          </p>
          <Button onClick={() => router.push(`/resumes/${resumeId}`)} className="w-full">
            {t('careerPositioning.backToResume')}
          </Button>
        </div>
      </div>
    );
  }

  const { positioning, risks, competencies, strategy, limitations } = positioningData;
  const activeVariant = strategy.narrative_variants.find((v) => v.type === selectedNarrative);
  const currentFitStyle = FIT_LEVEL_STYLES[positioning.fit_level] || {
    color: 'text-gray-600 border-gray-600',
    bg: 'bg-gray-50',
    text: 'text-gray-700',
  };

  return (
    <div className="min-h-screen bg-[#F6F5EE] py-12 px-4 md:px-8">
      <div className="max-w-7xl mx-auto space-y-8">
        {/* Header Block */}
        <div className="flex flex-col md:flex-row justify-between items-start md:items-center gap-4 border-2 border-black bg-white p-6 md:p-8 shadow-[4px_4px_0px_0px_rgba(0,0,0,1)]">
          <div className="space-y-2">
            <div className="flex gap-2">
              <Button
                variant="outline"
                onClick={() => router.push(`/resumes/${resumeId}`)}
                className="gap-2 mb-2"
              >
                <ArrowLeft className="w-4 h-4" />
                {t('careerPositioning.backToResume')}
              </Button>
              <Button
                variant="outline"
                onClick={() => triggerLoad(false, true)}
                disabled={refreshing}
                className="gap-2 mb-2 font-mono uppercase"
              >
                <RefreshCw className={`w-4 h-4 ${refreshing ? 'animate-spin' : ''}`} />
                {refreshing ? t('careerPositioning.loading') : t('careerPositioning.refresh')}
              </Button>
              <Button
                onClick={() => router.push(`/resumes/${resumeId}/positioning/plan`)}
                className="gap-2 mb-2 font-mono uppercase"
              >
                <ClipboardCheck className="w-4 h-4" />
                Przejrzyj plan CV
              </Button>
            </div>
            <h1 className="font-serif text-3xl md:text-4xl font-bold uppercase tracking-tight">
              {t('careerPositioning.title')}
            </h1>
            <p className="font-mono text-sm text-steel-grey">
              {t('careerPositioning.detectedLevel')}:{' '}
              <span className="font-bold text-black">{jobTitle}</span>
            </p>
          </div>

          <div className="flex flex-col gap-2 bg-[#F8F7F3] p-4 border border-black min-w-[280px] md:max-w-md">
            <div className="flex items-center gap-4">
              <div
                className={`text-4xl font-extrabold font-mono tracking-tighter ${currentFitStyle.color}`}
              >
                {positioning.fit_score}%
              </div>
              <div className="space-y-1">
                <div className="font-mono text-[10px] font-bold uppercase text-black">
                  {t('careerPositioning.seniorityFitLabel')}
                </div>
                <div className="font-mono text-[9px] uppercase leading-tight text-steel-grey">
                  {t('careerPositioning.confidenceLabel')}:{' '}
                  {Math.round(positioning.confidence * 100)}%
                </div>
                <div
                  className={`font-mono text-[10px] font-bold uppercase px-1.5 py-0.5 border ${currentFitStyle.color} ${currentFitStyle.bg} inline-block`}
                >
                  {translateFitLevel(positioning.fit_level)}
                </div>
              </div>
            </div>
            <p className="font-mono text-[9px] text-steel-grey leading-tight mt-1 border-t border-gray-200 pt-1">
              {t('careerPositioning.seniorityFitDescription')}
            </p>
          </div>
        </div>

        {/* Refresh Error Alert */}
        {refreshError && (
          <div className="border-2 border-red-500 bg-red-50 p-4 shadow-[3px_3px_0px_0px_rgba(239,68,68,1)] flex items-start gap-3">
            <AlertTriangle className="w-5 h-5 text-red-600 shrink-0 mt-0.5" />
            <div>
              <p className="font-mono text-sm font-bold uppercase tracking-wider text-red-800">
                {refreshError}
              </p>
            </div>
          </div>
        )}

        {/* Human Review Alert */}
        {positioning.requires_human_review && (
          <div className="border-2 border-red-500 bg-red-50 p-4 shadow-[3px_3px_0px_0px_rgba(239,68,68,1)] flex items-start gap-3">
            <ShieldAlert className="w-5 h-5 text-red-600 shrink-0 mt-0.5" />
            <div>
              <p className="font-mono text-sm font-bold uppercase tracking-wider text-red-800">
                {t('careerPositioning.humanReviewTitle')}
              </p>
              <p className="font-mono text-xs text-red-700 mt-1">
                {t('careerPositioning.humanReviewDescription')}
              </p>
            </div>
          </div>
        )}

        {/* Limitations warnings */}
        {limitations.messages.length > 0 && (
          <div className="border-2 border-amber-500 bg-amber-50 p-4 shadow-[3px_3px_0px_0px_rgba(245,158,11,1)]">
            <div className="flex items-start gap-3">
              <AlertTriangle className="w-5 h-5 text-amber-600 shrink-0 mt-0.5" />
              <div>
                <p className="font-mono text-sm font-bold uppercase tracking-wider text-amber-800">
                  {t('careerPositioning.limitations')}
                </p>
                <ul className="list-disc pl-4 mt-1 font-mono text-xs text-amber-700 space-y-1">
                  {limitations.messages.map((msg, i) => (
                    <li key={i}>{msg}</li>
                  ))}
                </ul>
              </div>
            </div>
          </div>
        )}

        <div className="grid grid-cols-1 lg:grid-cols-12 gap-8 items-start">
          <div className="lg:col-span-4 space-y-8">
            {/* Level Matrix */}
            <div className="border-2 border-black bg-white p-6 shadow-[4px_4px_0px_0px_rgba(0,0,0,1)] space-y-4">
              <h2 className="font-mono text-sm font-bold uppercase tracking-wider border-b border-black pb-2 flex items-center gap-2">
                <Compass className="w-4 h-4 text-blue-700" />
                {t('careerPositioning.jobMatrix')}
              </h2>

              <div className="grid grid-cols-1 gap-4 pt-2">
                <div className="space-y-1">
                  <span className="font-mono text-[10px] text-steel-grey uppercase block">
                    {t('careerPositioning.detectedLevel')}
                  </span>
                  <div
                    className={`font-mono text-xs font-bold uppercase border px-3 py-2 text-center ${LEVEL_COLORS[positioning.detected_job_level] || 'bg-gray-100 border-gray-300'}`}
                  >
                    {translateLevel(positioning.detected_job_level)}
                  </div>
                </div>

                <div className="space-y-1">
                  <span className="font-mono text-[10px] text-steel-grey uppercase block">
                    {t('careerPositioning.candidateLevel')}
                  </span>
                  <div
                    className={`font-mono text-xs font-bold uppercase border px-3 py-2 text-center ${LEVEL_COLORS[positioning.candidate_profile_level] || 'bg-gray-100 border-gray-300'}`}
                  >
                    {translateLevel(positioning.candidate_profile_level)}
                  </div>
                </div>
              </div>

              <div className="bg-[#F8F7F3] p-4 border border-black mt-4">
                <span className="font-mono text-[10px] text-steel-grey uppercase block mb-1">
                  {t('careerPositioning.fitType')}
                </span>
                <p className="font-mono text-xs font-bold uppercase text-blue-700">
                  {translateStrategy(positioning.positioning_strategy)}
                </p>
              </div>
            </div>

            {/* Risk Factors */}
            <div className="border-2 border-black bg-white p-6 shadow-[4px_4px_0px_0px_rgba(0,0,0,1)] space-y-4">
              <h2 className="font-mono text-sm font-bold uppercase tracking-wider border-b border-black pb-2 flex items-center gap-2">
                <ShieldAlert className="w-4 h-4 text-red-600" />
                {t('careerPositioning.risks')}
              </h2>

              <div className="space-y-3 pt-2">
                {/* Overqualification */}
                <div className="flex flex-col gap-1 border-b border-gray-100 pb-2">
                  <div className="flex justify-between items-center">
                    <span className="font-mono text-xs text-ink font-bold">
                      {t('careerPositioning.overqualification')}
                    </span>
                    <span
                      className={`font-mono text-[10px] uppercase font-bold border px-2 py-0.5 ${risks.overqualification.availability === 'AVAILABLE' && risks.overqualification.level ? RISK_COLORS[risks.overqualification.level] : 'bg-gray-100 text-gray-500 border-gray-200'}`}
                    >
                      {risks.overqualification.availability === 'AVAILABLE' &&
                      risks.overqualification.level
                        ? translateRiskLevel(risks.overqualification.level)
                        : t('careerPositioning.unsupportedRisk')}
                    </span>
                  </div>
                  {risks.overqualification.availability === 'AVAILABLE' &&
                    risks.overqualification.rationale.length > 0 && (
                      <p className="font-mono text-[10px] text-steel-grey mt-0.5 leading-snug">
                        {risks.overqualification.rationale.join(' ')}
                      </p>
                    )}
                </div>

                {/* Underqualification */}
                <div className="flex flex-col gap-1 border-b border-gray-100 pb-2">
                  <div className="flex justify-between items-center">
                    <span className="font-mono text-xs text-ink font-bold">
                      {t('careerPositioning.underqualification')}
                    </span>
                    <span
                      className={`font-mono text-[10px] uppercase font-bold border px-2 py-0.5 ${risks.underqualification.availability === 'AVAILABLE' && risks.underqualification.level ? RISK_COLORS[risks.underqualification.level] : 'bg-gray-100 text-gray-500 border-gray-200'}`}
                    >
                      {risks.underqualification.availability === 'AVAILABLE' &&
                      risks.underqualification.level
                        ? translateRiskLevel(risks.underqualification.level)
                        : t('careerPositioning.unsupportedRisk')}
                    </span>
                  </div>
                  {risks.underqualification.availability === 'AVAILABLE' &&
                    risks.underqualification.rationale.length > 0 && (
                      <p className="font-mono text-[10px] text-steel-grey mt-0.5 leading-snug">
                        {risks.underqualification.rationale.join(' ')}
                      </p>
                    )}
                </div>

                {/* Stability */}
                <div className="flex flex-col gap-1 border-b border-gray-100 pb-2">
                  <div className="flex justify-between items-center">
                    <span className="font-mono text-xs text-ink font-bold">
                      {t('careerPositioning.stabilityConcern')}
                    </span>
                    <span
                      className={`font-mono text-[10px] uppercase font-bold border px-2 py-0.5 ${risks.stability_concern.availability === 'AVAILABLE' && risks.stability_concern.level ? RISK_COLORS[risks.stability_concern.level] : 'bg-gray-100 text-gray-500 border-gray-200'}`}
                    >
                      {risks.stability_concern.availability === 'AVAILABLE' &&
                      risks.stability_concern.level
                        ? translateRiskLevel(risks.stability_concern.level)
                        : t('careerPositioning.unsupportedRisk')}
                    </span>
                  </div>
                  {risks.stability_concern.availability === 'AVAILABLE' &&
                  risks.stability_concern.rationale.length > 0 ? (
                    <p className="font-mono text-[10px] text-steel-grey mt-0.5 leading-snug">
                      {risks.stability_concern.rationale.join(' ')}
                    </p>
                  ) : (
                    <p className="font-mono text-[9px] text-gray-400 mt-0.5 italic leading-snug">
                      {t('careerPositioning.unsupportedRiskReason')}
                    </p>
                  )}
                </div>

                {/* Sector Gap */}
                <div className="flex flex-col gap-1 border-b border-gray-100 pb-2">
                  <div className="flex justify-between items-center">
                    <span className="font-mono text-xs text-ink font-bold">
                      {t('careerPositioning.sectorGap')}
                    </span>
                    <span
                      className={`font-mono text-[10px] uppercase font-bold border px-2 py-0.5 ${risks.sector_gap.availability === 'AVAILABLE' && risks.sector_gap.level ? RISK_COLORS[risks.sector_gap.level] : 'bg-gray-100 text-gray-500 border-gray-200'}`}
                    >
                      {risks.sector_gap.availability === 'AVAILABLE' && risks.sector_gap.level
                        ? translateRiskLevel(risks.sector_gap.level)
                        : t('careerPositioning.unsupportedRisk')}
                    </span>
                  </div>
                  {risks.sector_gap.availability === 'AVAILABLE' &&
                    risks.sector_gap.rationale.length > 0 && (
                      <p className="font-mono text-[10px] text-steel-grey mt-0.5 leading-snug">
                        {risks.sector_gap.rationale.join(' ')}
                      </p>
                    )}
                </div>

                {/* Functional Gap */}
                <div className="flex flex-col gap-1 pb-1">
                  <div className="flex justify-between items-center">
                    <span className="font-mono text-xs text-ink font-bold">
                      {t('careerPositioning.functionalGap')}
                    </span>
                    <span
                      className={`font-mono text-[10px] uppercase font-bold border px-2 py-0.5 ${risks.functional_gap.availability === 'AVAILABLE' && risks.functional_gap.level ? RISK_COLORS[risks.functional_gap.level] : 'bg-gray-100 text-gray-500 border-gray-200'}`}
                    >
                      {risks.functional_gap.availability === 'AVAILABLE' &&
                      risks.functional_gap.level
                        ? translateRiskLevel(risks.functional_gap.level)
                        : t('careerPositioning.unsupportedRisk')}
                    </span>
                  </div>
                  {risks.functional_gap.availability === 'AVAILABLE' &&
                    risks.functional_gap.rationale.length > 0 && (
                      <p className="font-mono text-[10px] text-steel-grey mt-0.5 leading-snug">
                        {risks.functional_gap.rationale.join(' ')}
                      </p>
                    )}
                </div>
              </div>
            </div>
          </div>

          <div className="lg:col-span-8 space-y-8">
            <RetroTabs
              tabs={[
                { id: 'overview', label: t('careerPositioning.tabOverview') },
                { id: 'competencies', label: t('careerPositioning.tabCompetencies') },
                { id: 'strategy', label: t('careerPositioning.tabStrategy') },
              ]}
              activeTab={activeSection}
              onTabChange={setActiveSection}
            />

            {/* Overview */}
            {activeSection === 'overview' && (
              <div className="border-2 border-black bg-white p-6 md:p-8 shadow-[4px_4px_0px_0px_rgba(0,0,0,1)] space-y-6">
                <div>
                  <h3 className="font-serif text-xl font-bold uppercase mb-2 flex items-center gap-2">
                    <Info className="w-5 h-5 text-blue-700" />
                    {t('careerPositioning.overallRecommendation')}
                  </h3>
                  <div className="font-mono text-xs text-steel-grey bg-[#F8F7F3] p-4 border border-black whitespace-pre-wrap leading-relaxed">
                    {strategy.recommendation || t('careerPositioning.emptyState')}
                  </div>
                </div>

                <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
                  <div className="border border-black p-3 text-center bg-[#F8F7F3]">
                    <div className="font-mono text-lg font-bold text-black">
                      {positioningData.evidence.used_truth_items_count}
                    </div>
                    <div className="font-mono text-[9px] uppercase text-steel-grey mt-1 leading-tight">
                      {t('careerPositioning.libraryEntries')}
                    </div>
                  </div>
                  <div className="border border-black p-3 text-center bg-[#F8F7F3]">
                    <div className="font-mono text-lg font-bold text-green-700">
                      {positioningData.evidence.direct_evidence_count}
                    </div>
                    <div className="font-mono text-[9px] uppercase text-steel-grey mt-1 leading-tight">
                      {t('careerPositioning.directMatches')}
                    </div>
                  </div>
                  <div className="border border-black p-3 text-center bg-[#F8F7F3]">
                    <div className="font-mono text-lg font-bold text-blue-700">
                      {positioningData.evidence.transferable_evidence_count}
                    </div>
                    <div className="font-mono text-[9px] uppercase text-steel-grey mt-1 leading-tight">
                      {t('careerPositioning.transferableMatches')}
                    </div>
                  </div>
                  <div className="border border-black p-3 text-center bg-[#F8F7F3]">
                    <div className="font-mono text-lg font-bold text-red-600">
                      {positioningData.evidence.unresolved_items_count}
                    </div>
                    <div className="font-mono text-[9px] uppercase text-steel-grey mt-1 leading-tight">
                      {t('careerPositioning.missingCritical')}
                    </div>
                  </div>
                </div>
              </div>
            )}

            {/* Competencies */}
            {activeSection === 'competencies' && (
              <div className="space-y-6">
                <div className="border-2 border-black bg-white p-6 shadow-[4px_4px_0px_0px_rgba(0,0,0,1)]">
                  <h3 className="font-mono text-sm font-bold uppercase tracking-wider border-b border-black pb-2 mb-4 flex items-center gap-2 text-green-800">
                    <CheckCircle2 className="w-4 h-4 text-green-700" />
                    {t('careerPositioning.directMatches')} ({competencies.direct_matches.length})
                  </h3>
                  {competencies.direct_matches.length === 0 ? (
                    <p className="font-mono text-xs text-steel-grey italic">
                      {t('careerPositioning.noDirectMatches')}
                    </p>
                  ) : (
                    <div className="space-y-3">
                      {competencies.direct_matches.map((item, idx) => (
                        <div key={idx} className="border border-green-200 bg-green-50/50 p-3">
                          <div className="font-mono text-xs font-bold text-green-900">
                            {item.name}
                          </div>
                          <div className="font-mono text-[11px] text-green-700 mt-1">
                            {item.reason}
                          </div>
                          <div className="font-mono text-[9px] text-steel-grey mt-1 uppercase">
                            {t('careerPositioning.sourceLabel', { source: item.source })}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                <div className="border-2 border-black bg-white p-6 shadow-[4px_4px_0px_0px_rgba(0,0,0,1)]">
                  <h3 className="font-mono text-sm font-bold uppercase tracking-wider border-b border-black pb-2 mb-4 flex items-center gap-2 text-blue-800">
                    <TrendingUp className="w-4 h-4 text-blue-700" />
                    {t('careerPositioning.transferableMatches')} (
                    {competencies.transferable_matches.length})
                  </h3>
                  {competencies.transferable_matches.length === 0 ? (
                    <p className="font-mono text-xs text-steel-grey italic">
                      {t('careerPositioning.noTransferableMatches')}
                    </p>
                  ) : (
                    <div className="space-y-3">
                      {competencies.transferable_matches.map((item, idx) => (
                        <div key={idx} className="border border-blue-200 bg-blue-50/50 p-3">
                          <div className="font-mono text-xs font-bold text-blue-900">
                            {item.name}
                          </div>
                          <div className="font-mono text-[11px] text-blue-700 mt-1">
                            {item.reason}
                          </div>
                          <div className="font-mono text-[9px] text-steel-grey mt-1 uppercase">
                            {t('careerPositioning.sourceLabel', { source: item.source })}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                <div className="border-2 border-black bg-white p-6 shadow-[4px_4px_0px_0px_rgba(0,0,0,1)]">
                  <h3 className="font-mono text-sm font-bold uppercase tracking-wider border-b border-black pb-2 mb-4 flex items-center gap-2 text-red-800">
                    <ShieldAlert className="w-4 h-4 text-red-700" />
                    {t('careerPositioning.missingCritical')} ({competencies.missing_critical.length}
                    )
                  </h3>
                  {competencies.missing_critical.length === 0 ? (
                    <p className="font-mono text-xs text-green-700 italic flex items-center gap-1">
                      <CheckCircle2 className="w-4 h-4 text-green-600" />{' '}
                      {t('careerPositioning.noMissingCritical')}
                    </p>
                  ) : (
                    <div className="space-y-3">
                      {competencies.missing_critical.map((item, idx) => (
                        <div key={idx} className="border border-red-200 bg-red-50/50 p-3">
                          <div className="font-mono text-xs font-bold text-red-900">
                            {item.name}
                          </div>
                          <div className="font-mono text-[11px] text-red-700 mt-1">
                            {item.reason}
                          </div>
                          <div className="font-mono text-[9px] text-steel-grey mt-1 uppercase">
                            {t('careerPositioning.sourceLabel', { source: item.source })}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>

                <div className="border-2 border-black bg-white p-6 shadow-[4px_4px_0px_0px_rgba(0,0,0,1)]">
                  <h3 className="font-mono text-sm font-bold uppercase tracking-wider border-b border-black pb-2 mb-4 flex items-center gap-2 text-amber-800">
                    <AlertTriangle className="w-4 h-4 text-amber-600" />
                    {t('careerPositioning.excessiveForRole')} (
                    {competencies.excessive_for_role.length})
                  </h3>
                  {competencies.excessive_for_role.length === 0 ? (
                    <p className="font-mono text-xs text-steel-grey italic">
                      {t('careerPositioning.noExcessiveForRole')}
                    </p>
                  ) : (
                    <div className="space-y-3">
                      {competencies.excessive_for_role.map((item, idx) => (
                        <div key={idx} className="border border-amber-200 bg-amber-50/50 p-3">
                          <div className="font-mono text-xs font-bold text-amber-900">
                            {item.name}
                          </div>
                          <div className="font-mono text-[11px] text-amber-700 mt-1">
                            {item.reason}
                          </div>
                          <div className="font-mono text-[9px] text-steel-grey mt-1 uppercase">
                            {t('careerPositioning.sourceLabel', { source: item.source })}
                          </div>
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              </div>
            )}

            {/* Strategy */}
            {activeSection === 'strategy' && (
              <div className="space-y-6">
                <div className="flex gap-2 bg-white border-2 border-black p-2 shadow-[2px_2px_0px_0px_rgba(0,0,0,1)]">
                  {(['EXPERT', 'MANAGER', 'DIRECTOR'] as const).map((variant) => {
                    const isRecommended = strategy.recommended_narrative === variant;
                    const isSelected = selectedNarrative === variant;

                    return (
                      <button
                        key={variant}
                        onClick={() => setSelectedNarrative(variant)}
                        className={`flex-1 py-2 px-3 font-mono text-[11px] font-bold uppercase border transition-all flex items-center justify-center gap-1.5 ${
                          isSelected
                            ? 'bg-black text-white border-black'
                            : 'bg-white text-black border-gray-300 hover:border-black'
                        }`}
                      >
                        {t(`careerPositioning.${variant.toLowerCase()}`)}
                        {isRecommended && (
                          <Sparkles
                            className={`w-3.5 h-3.5 ${isSelected ? 'text-yellow-400' : 'text-blue-700'}`}
                          />
                        )}
                      </button>
                    );
                  })}
                </div>

                {activeVariant && (
                  <div className="border-2 border-black bg-white p-6 md:p-8 shadow-[4px_4px_0px_0px_rgba(0,0,0,1)] space-y-6">
                    <div className="flex justify-between items-center border-b border-black pb-4">
                      <div>
                        <h4 className="font-serif text-lg font-bold uppercase">
                          {t('careerPositioning.variantLabel')}:{' '}
                          {t(`careerPositioning.${selectedNarrative.toLowerCase()}`)}
                        </h4>
                        <p className="font-mono text-[10px] text-steel-grey mt-1">
                          {t('careerPositioning.strategy')}
                        </p>
                      </div>
                      {activeVariant.availability === 'UNSUPPORTED' ? (
                        <span className="font-mono text-[9px] uppercase font-bold bg-red-100 text-red-800 border border-red-300 px-2.5 py-1">
                          {t('careerPositioning.unsupportedVariant')}
                        </span>
                      ) : (
                        strategy.recommended_narrative === selectedNarrative && (
                          <span className="font-mono text-[9px] uppercase font-bold bg-blue-100 text-blue-800 border border-blue-300 px-2.5 py-1 flex items-center gap-1">
                            <CheckCircle2 className="w-3 h-3 text-blue-700" />
                            {t('careerPositioning.recommended')}
                          </span>
                        )
                      )}
                    </div>

                    {activeVariant.availability === 'UNSUPPORTED' ? (
                      <div className="border border-red-300 bg-red-50 p-4 font-mono text-xs text-red-700 space-y-2">
                        <p className="font-bold">
                          {t('careerPositioning.unsupportedVariantReason')}
                        </p>
                        {activeVariant.limitations.length > 0 && (
                          <ul className="list-disc pl-4 space-y-1 text-[11px]">
                            {activeVariant.limitations.map((limMsg, idx) => (
                              <li key={idx}>{limMsg}</li>
                            ))}
                          </ul>
                        )}
                      </div>
                    ) : (
                      <>
                        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                          <div className="border border-black p-4 bg-[#F8F7F3]">
                            <span className="font-mono text-[10px] text-steel-grey uppercase block mb-1">
                              {t('careerPositioning.titlePositioning')}
                            </span>
                            <p className="font-mono text-xs font-bold text-ink leading-relaxed">
                              {activeVariant.title_positioning}
                            </p>
                          </div>

                          <div className="border border-black p-4 bg-[#F8F7F3]">
                            <span className="font-mono text-[10px] text-steel-grey uppercase block mb-1">
                              {t('careerPositioning.summaryPositioning')}
                            </span>
                            <p className="font-mono text-xs font-bold text-ink leading-relaxed">
                              {activeVariant.summary_positioning}
                            </p>
                          </div>
                        </div>

                        <div className="space-y-4 pt-2">
                          <div>
                            <span className="font-mono text-[10px] font-bold text-green-800 uppercase block mb-2">
                              ✦ {t('careerPositioning.whatToEmphasize')}
                            </span>
                            <ul className="space-y-1.5">
                              {activeVariant.elements_to_emphasize.map((item, idx) => (
                                <li
                                  key={idx}
                                  className="font-mono text-xs text-ink flex items-start gap-2"
                                >
                                  <span className="text-green-600 font-bold shrink-0">•</span>
                                  <span>{item}</span>
                                </li>
                              ))}
                            </ul>
                          </div>

                          <div className="pt-2">
                            <span className="font-mono text-[10px] font-bold text-amber-800 uppercase block mb-2">
                              ✦ {t('careerPositioning.whatToFlatten')}
                            </span>
                            {activeVariant.elements_to_flatten.length === 0 ? (
                              <p className="font-mono text-[11px] text-steel-grey italic">
                                {t('careerPositioning.emptyState')}
                              </p>
                            ) : (
                              <ul className="space-y-1.5">
                                {activeVariant.elements_to_flatten.map((item, idx) => (
                                  <li
                                    key={idx}
                                    className="font-mono text-xs text-ink flex items-start gap-2"
                                  >
                                    <span className="text-amber-500 font-bold shrink-0">•</span>
                                    <span>{item}</span>
                                  </li>
                                ))}
                              </ul>
                            )}
                          </div>

                          <div className="pt-2">
                            <span className="font-mono text-[10px] font-bold text-red-800 uppercase block mb-2">
                              ✦ {t('careerPositioning.whatToRemove')}
                            </span>
                            {activeVariant.elements_to_remove_or_deprioritize.length === 0 ? (
                              <p className="font-mono text-[11px] text-steel-grey italic">
                                {t('careerPositioning.emptyState')}
                              </p>
                            ) : (
                              <ul className="space-y-1.5">
                                {activeVariant.elements_to_remove_or_deprioritize.map(
                                  (item, idx) => (
                                    <li
                                      key={idx}
                                      className="font-mono text-xs text-ink flex items-start gap-2"
                                    >
                                      <span className="text-red-500 font-bold shrink-0">•</span>
                                      <span>{item}</span>
                                    </li>
                                  )
                                )}
                              </ul>
                            )}
                          </div>
                        </div>
                      </>
                    )}
                  </div>
                )}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
