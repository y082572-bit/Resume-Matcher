'use client';

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { AlertTriangle, ArrowLeft, RefreshCw, Sparkles } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { fetchResumeJobContext, fetchResumePositioningContext } from '@/lib/api/career-positioning';
import { fetchCVTransformationPlan } from '@/lib/api/cv-transformation-plan';
import { fetchCVTransformationPlanApproval } from '@/lib/api/cv-transformation-approval';
import { CVTransformationPlanApprovalApiError } from '@/lib/api/cv-transformation-approval';
import {
  CVTransformationGenerationApiError,
  fetchCurrentCVTransformationGeneration,
  generateCVFromApprovedPlan,
} from '@/lib/api/cv-transformation-generation';
import type { CVTransformationPlan } from '@/lib/types/cv-transformation-plan';
import type { CVTransformationPlanApproval } from '@/lib/types/cv-transformation-approval';
import type { CVTransformationGeneration } from '@/lib/types/cv-transformation-generation';

function message(error: unknown): string {
  if (error instanceof CVTransformationGenerationApiError) {
    if (error.status === 409 && error.code === 'GENERATION_IN_PROGRESS')
      return 'Generowanie już trwa. Odśwież status za chwilę.';
    if (error.status === 409) return 'Plan lub źródło zmieniło się. Wróć do planu i odśwież dane.';
    if (error.code === 'NO_TRANSFORMATION_ITEMS') return 'Plan nie zawiera elementów do generacji.';
    if (error.status === 422) return 'Approval nie spełnia warunków bezpiecznej generacji.';
    if (error.status === 502) return 'Model zwrócił niebezpieczną albo nieprawidłową odpowiedź.';
    if (error.status === 503) return 'Skonfiguruj provider i model LLM przed generowaniem.';
    return error.message;
  }
  return 'Nie udało się pobrać lub wygenerować kontrolowanego draftu.';
}

export default function ApprovedPlanGenerationPage() {
  const params = useParams();
  const router = useRouter();
  const resumeId = typeof params?.id === 'string' ? params.id : '';
  const controllerRef = useRef<AbortController | null>(null);
  const submittingRef = useRef(false);
  const [jobId, setJobId] = useState('');
  const [jobTitle, setJobTitle] = useState('Oferta pracy');
  const [plan, setPlan] = useState<CVTransformationPlan | null>(null);
  const [approval, setApproval] = useState<CVTransformationPlanApproval | null>(null);
  const [generation, setGeneration] = useState<CVTransformationGeneration | null>(null);
  const [loading, setLoading] = useState(true);
  const [generating, setGenerating] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!resumeId) return;
    controllerRef.current?.abort();
    const controller = new AbortController();
    controllerRef.current = controller;
    setLoading(true);
    setError(null);
    setGeneration(null);
    try {
      await fetchResumePositioningContext(resumeId, { signal: controller.signal });
      const context = await fetchResumeJobContext(resumeId, { signal: controller.signal });
      const nextPlan = await fetchCVTransformationPlan(context.job_id, resumeId, controller.signal);
      let nextApproval: CVTransformationPlanApproval | null = null;
      try {
        nextApproval = await fetchCVTransformationPlanApproval(
          context.job_id,
          resumeId,
          controller.signal
        );
      } catch (approvalError) {
        if (
          !(
            approvalError instanceof CVTransformationPlanApprovalApiError &&
            approvalError.status === 404
          )
        )
          throw approvalError;
      }
      let nextGeneration: CVTransformationGeneration | null = null;
      try {
        nextGeneration = await fetchCurrentCVTransformationGeneration(
          context.job_id,
          resumeId,
          controller.signal
        );
      } catch (generationError) {
        if (
          !(
            generationError instanceof CVTransformationGenerationApiError &&
            generationError.status === 404
          )
        )
          throw generationError;
      }
      if (controller.signal.aborted) return;
      setJobId(context.job_id);
      setJobTitle(context.job_title ?? `Oferta ${context.job_id}`);
      setPlan(nextPlan);
      setApproval(nextApproval);
      setGeneration(nextGeneration);
    } catch (loadError) {
      if (!(loadError instanceof Error && loadError.name === 'AbortError'))
        setError(message(loadError));
    } finally {
      if (!controller.signal.aborted) setLoading(false);
    }
  }, [resumeId]);

  useEffect(() => {
    void load();
    return () => controllerRef.current?.abort();
  }, [load]);

  const generate = async (retryFailed = false) => {
    if (
      !plan ||
      !approval ||
      !jobId ||
      approval.status !== 'APPROVED' ||
      submittingRef.current ||
      loading ||
      generating ||
      error
    )
      return;
    submittingRef.current = true;
    setGenerating(true);
    setError(null);
    setGeneration(null);
    const controller = new AbortController();
    controllerRef.current = controller;
    try {
      const result = await generateCVFromApprovedPlan(
        jobId,
        resumeId,
        {
          approval_id: approval.approval_id,
          plan_fingerprint: plan.plan_fingerprint,
          retry_failed: retryFailed,
        },
        controller.signal
      );
      setGeneration(result);
    } catch (generationError) {
      if (!(generationError instanceof Error && generationError.name === 'AbortError')) {
        setGeneration(null);
        setError(message(generationError));
      }
    } finally {
      submittingRef.current = false;
      setGenerating(false);
    }
  };

  const planItems = useMemo(
    () =>
      plan
        ? [
            ...plan.summary_plan,
            ...plan.experience_plan,
            ...plan.competencies_plan,
            ...plan.achievements_plan,
            ...plan.tools_plan,
          ]
        : [],
    [plan]
  );
  const displayLabels = useMemo(
    () => new Map(planItems.map((item) => [item.source_reference, item.display_label])),
    [planItems]
  );
  const draft =
    !loading && !generating && !error && generation?.status === 'GENERATED'
      ? generation.draft_resume
      : null;
  const competencySectionKey = draft?.sectionMeta?.find(
    (section) =>
      /^careerPositioningCompetencies\d*$/.test(section.key) &&
      section.id === `system:approved-plan-generation:${section.key}` &&
      section.displayName === 'Kluczowe kompetencje' &&
      section.sectionType === 'stringList' &&
      section.isDefault === false
  )?.key;
  const competencies = competencySectionKey
    ? (draft?.customSections?.[competencySectionKey]?.strings ?? [])
    : [];
  const blocked = !approval || approval.status !== 'APPROVED' || planItems.length === 0;

  return (
    <main className="min-h-screen bg-[#F6F5EE] px-4 py-8 md:px-8" aria-busy={loading || generating}>
      <div className="mx-auto max-w-6xl space-y-6">
        <header className="border-2 border-black bg-white p-6 shadow-[4px_4px_0_0_#000]">
          <Button
            variant="outline"
            className="mb-4 gap-2"
            onClick={() => router.push(`/resumes/${resumeId}/positioning/plan`)}
          >
            <ArrowLeft className="h-4 w-4" /> Wróć do planu
          </Button>
          <h1 className="font-serif text-3xl font-bold uppercase">Kontrolowane generowanie CV</h1>
          <p className="mt-2 font-mono text-sm">{jobTitle}</p>
          <p className="mt-3 flex items-start gap-2 border-2 border-amber-700 bg-amber-50 p-4 font-mono text-sm">
            <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0" />
            To draft wygenerowany przez AI wyłącznie z zatwierdzonych elementów. Wymaga osobnej
            walidacji prawdy w Etapie 10D.
          </p>
        </header>

        {(loading || generating) && (
          <p
            className="border-2 border-black bg-white p-4 font-mono"
            role="status"
            aria-live="polite"
          >
            {generating
              ? 'Model generuje kontrolowany draft…'
              : 'Ładowanie planu, approval i generacji…'}
          </p>
        )}
        {error && (
          <p
            className="border-2 border-red-700 bg-red-50 p-4 font-mono text-red-800"
            role="alert"
            aria-live="assertive"
          >
            {error}
          </p>
        )}
        {blocked && !loading && (
          <p className="border-2 border-amber-700 bg-amber-50 p-4 font-mono" role="status">
            {!approval
              ? 'Nie znaleziono approval dla aktualnego planu.'
              : planItems.length === 0
                ? 'Plan nie zawiera elementów do generacji.'
                : 'Generowanie jest dostępne wyłącznie dla aktualnego approval o statusie APPROVED.'}
          </p>
        )}

        {!loading && !generation && !blocked && (
          <section className="border-2 border-black bg-white p-6">
            <h2 className="font-serif text-2xl font-bold">Brak draftu</h2>
            <p className="mt-2 font-mono text-sm">
              Rozpocznij jedną kontrolowaną generację dla aktualnego fingerprintu.
            </p>
            <Button
              className="mt-4 gap-2"
              disabled={loading || generating || Boolean(error)}
              onClick={() => void generate(false)}
            >
              <Sparkles className="h-4 w-4" /> Generuj draft
            </Button>
          </section>
        )}

        {generation && !loading && !error && (
          <section className="border-2 border-black bg-white p-6">
            <h2 className="font-serif text-2xl font-bold">Status: {generation.status}</h2>
            <dl className="mt-3 grid gap-2 font-mono text-xs md:grid-cols-4">
              <div>
                <dt>Provider</dt>
                <dd className="font-bold">{generation.provider}</dd>
              </div>
              <div>
                <dt>Model</dt>
                <dd className="font-bold">{generation.model}</dd>
              </div>
              <div>
                <dt>Prompt version</dt>
                <dd className="font-bold">{generation.prompt_version}</dd>
              </div>
              <div>
                <dt>Truth validation</dt>
                <dd className="font-bold">{generation.truth_validation_status}</dd>
              </div>
              <div>
                <dt>Plan fingerprint</dt>
                <dd className="font-bold">{generation.plan_fingerprint.slice(0, 12)}</dd>
              </div>
              <div>
                <dt>Approval</dt>
                <dd className="font-bold">{generation.approval_id.slice(0, 12)}</dd>
              </div>
            </dl>
            {generation.status === 'FAILED' && (
              <Button
                className="mt-4"
                disabled={loading || generating || Boolean(error)}
                onClick={() => void generate(true)}
              >
                Ponów nieudaną generację
              </Button>
            )}
            {generation.status === 'GENERATING' && (
              <Button
                className="mt-4 gap-2"
                variant="outline"
                disabled={loading || generating}
                onClick={() => void load()}
              >
                <RefreshCw className="h-4 w-4" /> Odśwież status
              </Button>
            )}
          </section>
        )}

        {draft && (
          <div className="space-y-5">
            <section className="border-2 border-black bg-white p-6">
              <h2 className="font-serif text-2xl font-bold">Profil</h2>
              <p className="mt-3 whitespace-pre-wrap font-mono text-sm">{draft.summary}</p>
            </section>
            <section className="border-2 border-black bg-white p-6">
              <h2 className="font-serif text-2xl font-bold">Doświadczenie i osiągnięcia</h2>
              <div className="mt-4 space-y-4">
                {draft.workExperience?.map((item) => (
                  <article
                    key={`${item.id}-${item.company}-${item.years}`}
                    className="border border-black p-4"
                  >
                    <h3 className="font-serif text-lg font-bold">
                      {item.title} — {item.company}
                    </h3>
                    <p className="font-mono text-xs">{item.years}</p>
                    <ul className="mt-2 list-disc pl-5 font-mono text-sm">
                      {item.description?.map((line) => (
                        <li key={line}>{line}</li>
                      ))}
                    </ul>
                  </article>
                ))}
              </div>
            </section>
            <section className="grid gap-5 md:grid-cols-3">
              <div className="border-2 border-black bg-white p-6">
                <h2 className="font-serif text-2xl font-bold">Kluczowe kompetencje</h2>
                <ul className="mt-3 list-disc pl-5 font-mono text-sm">
                  {competencies.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
              <div className="border-2 border-black bg-white p-6">
                <h2 className="font-serif text-2xl font-bold">Narzędzia techniczne</h2>
                <ul className="mt-3 list-disc pl-5 font-mono text-sm">
                  {draft.additional?.technicalSkills?.map((item) => (
                    <li key={item}>{item}</li>
                  ))}
                </ul>
              </div>
              <div className="border-2 border-black bg-white p-6">
                <h2 className="font-serif text-2xl font-bold">Pozostałe zachowane sekcje</h2>
                <p className="mt-3 font-mono text-sm">
                  Edukacja: {draft.education?.length ?? 0} · Języki:{' '}
                  {draft.additional?.languages?.length ?? 0} · Certyfikaty:{' '}
                  {draft.additional?.certificationsTraining?.length ?? 0}
                </p>
              </div>
            </section>
            <section className="border-2 border-black bg-white p-6">
              <h2 className="font-serif text-2xl font-bold">Provenance</h2>
              <ul className="mt-4 space-y-2 font-mono text-xs">
                {generation?.provenance.map((item) => (
                  <li key={item.source_reference} className="border border-black p-3">
                    <strong>
                      {displayLabels.get(item.source_reference) ?? item.source_reference}
                    </strong>
                    <span className="block">{item.source_reference}</span>
                    <span className="block">
                      {item.section} · {item.action} · {item.mode} · {item.priority} · LLM:{' '}
                      {item.llm_used ? 'TAK' : 'NIE'}
                    </span>
                    <span className="block">
                      Truth validation: {item.validation_status} · Boundary validation:{' '}
                      {item.boundary_validation_status}
                    </span>
                    {item.mode === 'COPIED_PROTECTED_CONTENT' && (
                      <span className="mt-1 block font-bold">
                        Zachowano dokładną treść źródłową, ponieważ element zawiera twierdzenie
                        chronione bez permission do przeformułowania.
                      </span>
                    )}
                  </li>
                ))}
              </ul>
            </section>
          </div>
        )}

        <footer className="flex flex-wrap gap-3 border-2 border-black bg-white p-4">
          <Button variant="outline" onClick={() => void load()} disabled={loading || generating}>
            Odśwież
          </Button>
          <Button
            variant="outline"
            onClick={() => router.push(`/resumes/${resumeId}/positioning/plan`)}
          >
            Wróć do planu
          </Button>
          <Button disabled>Waliduj i zastosuj — Etap 10D</Button>
        </footer>
      </div>
    </main>
  );
}
