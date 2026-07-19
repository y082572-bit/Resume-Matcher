'use client';

import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { AlertTriangle, ArrowLeft, RefreshCw, ShieldCheck } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { fetchResumeJobContext, fetchResumePositioningContext } from '@/lib/api/career-positioning';
import {
  CVTransformationPlanApiError,
  fetchCVTransformationPlan,
} from '@/lib/api/cv-transformation-plan';
import {
  CVTransformationPlanApprovalApiError,
  fetchCVTransformationPlanApproval,
  saveCVTransformationPlanApproval,
} from '@/lib/api/cv-transformation-approval';
import type {
  CVTransformationPlanApproval,
  TransformationPlanDecisionValue,
} from '@/lib/types/cv-transformation-approval';
import type {
  CVTransformationPlan,
  CVTransformationPlanItem,
  CVTransformationSection,
} from '@/lib/types/cv-transformation-plan';

const DECISION_OPTIONS: Array<{
  value: TransformationPlanDecisionValue;
  label: string;
  description: string;
}> = [
  { value: 'ACCEPT', label: 'Akceptuję', description: 'Użyj dokładnie rekomendowanej akcji.' },
  { value: 'REJECT', label: 'Odrzucam', description: 'Nie używaj tego elementu.' },
  {
    value: 'REQUEST_REVIEW',
    label: 'Proszę o przegląd',
    description: 'Pozostaw element nierozstrzygnięty.',
  },
];

const SECTIONS: Array<{
  key: keyof Pick<
    CVTransformationPlan,
    'summary_plan' | 'experience_plan' | 'competencies_plan' | 'achievements_plan' | 'tools_plan'
  >;
  section: CVTransformationSection;
  label: string;
}> = [
  { key: 'summary_plan', section: 'SUMMARY', label: 'Profil' },
  { key: 'experience_plan', section: 'EXPERIENCE', label: 'Doświadczenie' },
  { key: 'competencies_plan', section: 'COMPETENCIES', label: 'Kompetencje' },
  { key: 'achievements_plan', section: 'ACHIEVEMENTS', label: 'Osiągnięcia' },
  { key: 'tools_plan', section: 'TOOLS', label: 'Narzędzia' },
];

function allItems(plan: CVTransformationPlan): CVTransformationPlanItem[] {
  return SECTIONS.flatMap(({ key }) => plan[key]);
}

function requestErrorMessage(error: unknown): string {
  const status =
    error instanceof CVTransformationPlanApiError ||
    error instanceof CVTransformationPlanApprovalApiError
      ? error.status
      : 0;
  if (status === 404) return 'Nie znaleziono planu, CV albo powiązanej oferty.';
  if (status === 409) return 'Plan jest nieaktualny. Odśwież go przed zapisem.';
  if (status === 422) return 'Backend odrzucił niespójne decyzje.';
  return 'Nie udało się połączyć z usługą planu transformacji.';
}

export default function TransformationPlanReviewPage() {
  const params = useParams();
  const router = useRouter();
  const resumeId = typeof params?.id === 'string' ? params.id : '';
  const controllerRef = useRef<AbortController | null>(null);
  const submittingRef = useRef(false);

  const [plan, setPlan] = useState<CVTransformationPlan | null>(null);
  const [approval, setApproval] = useState<CVTransformationPlanApproval | null>(null);
  const [jobTitle, setJobTitle] = useState('Oferta pracy');
  const [decisions, setDecisions] = useState<Record<string, TransformationPlanDecisionValue>>({});
  const [guardrailsAcknowledged, setGuardrailsAcknowledged] = useState(false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [stale, setStale] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [validationError, setValidationError] = useState<string | null>(null);

  const load = useCallback(
    async (intentionalRefresh = false) => {
      if (!resumeId) return;
      controllerRef.current?.abort();
      const controller = new AbortController();
      controllerRef.current = controller;
      setLoading(true);
      setError(null);
      try {
        await fetchResumePositioningContext(resumeId, { signal: controller.signal });
        const context = await fetchResumeJobContext(resumeId, { signal: controller.signal });
        const nextPlan = await fetchCVTransformationPlan(
          context.job_id,
          resumeId,
          controller.signal
        );
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
          ) {
            throw approvalError;
          }
        }
        if (controller.signal.aborted) return;
        setPlan(nextPlan);
        setJobTitle(context.job_title ?? `Oferta ${context.job_id}`);
        setApproval(nextApproval);
        const approvalIsCurrent =
          nextApproval?.plan_fingerprint === nextPlan.plan_fingerprint &&
          nextApproval.status !== 'SUPERSEDED';
        if (approvalIsCurrent && nextApproval) {
          setDecisions(
            Object.fromEntries(
              nextApproval.decisions.map((item) => [item.source_reference, item.decision])
            )
          );
          setGuardrailsAcknowledged(nextApproval.guardrails_acknowledged);
        } else if (intentionalRefresh || !plan) {
          setDecisions({});
          setGuardrailsAcknowledged(false);
        }
        setStale(false);
        setValidationError(null);
      } catch (loadError) {
        if (controller.signal.aborted) return;
        setError(requestErrorMessage(loadError));
      } finally {
        if (!controller.signal.aborted) setLoading(false);
      }
    },
    [plan, resumeId]
  );

  useEffect(() => {
    void load();
    return () => controllerRef.current?.abort();
  }, [resumeId]); // eslint-disable-line react-hooks/exhaustive-deps

  const items = useMemo(() => (plan ? allItems(plan) : []), [plan]);
  const missingDecision = items.some((item) => !decisions[item.source_reference]);
  const canApprove = Boolean(
    plan &&
    items.length > 0 &&
    !missingDecision &&
    guardrailsAcknowledged &&
    !stale &&
    !loading &&
    !saving &&
    !error &&
    !validationError
  );

  const save = async (submit: boolean) => {
    if (!plan || loading || saving || error || submittingRef.current) return;
    if (submit && (missingDecision || !guardrailsAcknowledged || stale)) {
      setValidationError(
        stale
          ? 'Odśwież nieaktualny plan.'
          : 'Rozstrzygnij wszystkie elementy i potwierdź guardraile.'
      );
      return;
    }
    submittingRef.current = true;
    setSaving(true);
    setValidationError(null);
    try {
      const saved = await saveCVTransformationPlanApproval(plan.job_id, resumeId, {
        plan_fingerprint: plan.plan_fingerprint,
        decisions: Object.entries(decisions)
          .map(([source_reference, decision]) => ({ source_reference, decision }))
          .sort((left, right) => left.source_reference.localeCompare(right.source_reference)),
        guardrails_acknowledged: guardrailsAcknowledged,
        submit,
      });
      setApproval(saved);
      setStale(saved.status === 'SUPERSEDED');
    } catch (saveError) {
      if (saveError instanceof CVTransformationPlanApprovalApiError && saveError.status === 409) {
        setStale(true);
      }
      setValidationError(requestErrorMessage(saveError));
    } finally {
      submittingRef.current = false;
      setSaving(false);
    }
  };

  if (loading && !plan) {
    return (
      <main className="min-h-screen bg-[#F6F5EE] p-8" aria-busy="true">
        <p className="font-mono" role="status">
          Ładowanie planu transformacji…
        </p>
      </main>
    );
  }

  if (error && !plan) {
    return (
      <main className="min-h-screen bg-[#F6F5EE] p-8">
        <div className="mx-auto max-w-2xl border-2 border-black bg-white p-6">
          <h1 className="font-serif text-2xl font-bold">Plan transformacji CV</h1>
          <p className="mt-4 font-mono text-sm text-red-700" role="alert" aria-live="assertive">
            {error}
          </p>
          <div className="mt-6 flex gap-3">
            <Button onClick={() => void load()}>Spróbuj ponownie</Button>
            <Button
              variant="outline"
              onClick={() => router.push(`/resumes/${resumeId}/positioning`)}
            >
              Wróć do Career Positioning
            </Button>
          </div>
        </div>
      </main>
    );
  }

  if (!plan) return null;

  return (
    <main className="min-h-screen bg-[#F6F5EE] px-4 py-8 md:px-8">
      <div className="mx-auto max-w-6xl space-y-6">
        <header className="border-2 border-black bg-white p-6 shadow-[4px_4px_0_0_#000]">
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div>
              <Button
                variant="outline"
                className="mb-4 gap-2"
                onClick={() => router.push(`/resumes/${resumeId}/positioning`)}
              >
                <ArrowLeft className="h-4 w-4" /> Wróć do Career Positioning
              </Button>
              <h1 className="font-serif text-3xl font-bold uppercase">Plan transformacji CV</h1>
              <p className="mt-2 font-mono text-sm">{jobTitle}</p>
            </div>
            <Button
              variant="outline"
              className="gap-2"
              disabled={loading || saving}
              onClick={() => void load(true)}
            >
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} /> Odśwież plan
            </Button>
          </div>
          <dl className="mt-6 grid grid-cols-2 gap-3 font-mono text-xs md:grid-cols-5">
            <div>
              <dt>Poziom oferty</dt>
              <dd className="font-bold">{plan.detected_job_level}</dd>
            </div>
            <div>
              <dt>Poziom kandydata</dt>
              <dd className="font-bold">{plan.candidate_profile_level}</dd>
            </div>
            <div>
              <dt>Strategia</dt>
              <dd className="font-bold">{plan.positioning_strategy}</dd>
            </div>
            <div>
              <dt>Wersja</dt>
              <dd className="font-bold">{plan.plan_version}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd className="font-bold">{approval?.status ?? 'NIEZAPISANY'}</dd>
            </div>
          </dl>
        </header>

        {(stale || validationError || error) && (
          <div
            className="border-2 border-red-600 bg-red-50 p-4 font-mono text-sm text-red-800"
            role="alert"
            aria-live="assertive"
          >
            {stale
              ? 'Plan zmienił się po zmianie CV, oferty lub danych źródłowych. Świadomie odśwież plan; stare decyzje nie zostaną przeniesione.'
              : (validationError ?? error)}
          </div>
        )}

        <section className="border-2 border-black bg-white p-6">
          <h2 className="font-serif text-xl font-bold uppercase">Podsumowanie</h2>
          <p className="mt-2 font-mono text-sm">
            Wymaga przeglądu: <strong>{plan.requires_human_review ? 'TAK' : 'NIE'}</strong>
          </p>
          <p className="mt-2 font-mono text-xs">
            Źródła: {plan.source_summary.resume_items_count} elementów CV,{' '}
            {plan.source_summary.job_requirements_count} wymagań,{' '}
            {plan.source_summary.approved_truth_items_count} zatwierdzonych wpisów.
          </p>
          {plan.limitations.length > 0 && (
            <ul className="mt-3 list-disc pl-5 font-mono text-xs">
              {plan.limitations.map((limitation) => (
                <li key={limitation}>{limitation}</li>
              ))}
            </ul>
          )}
        </section>

        {items.length === 0 ? (
          <section className="border-2 border-black bg-white p-6" role="status">
            Plan nie zawiera elementów do rozstrzygnięcia.
          </section>
        ) : (
          SECTIONS.map(({ key, label }) => {
            const sectionItems = plan[key];
            if (sectionItems.length === 0) return null;
            return (
              <section key={key} aria-labelledby={`${key}-heading`} className="space-y-4">
                <h2 id={`${key}-heading`} className="font-serif text-2xl font-bold uppercase">
                  {label}
                </h2>
                {sectionItems.map((item) => {
                  const permissions = plan.evidence_permissions.filter(
                    (permission) => permission.source_reference === item.source_reference
                  );
                  return (
                    <article
                      key={item.source_reference}
                      className="border-2 border-black bg-white p-5"
                    >
                      <div className="flex flex-wrap items-start justify-between gap-3">
                        <div>
                          <h3 className="font-serif text-lg font-bold">{item.display_label}</h3>
                          <p className="mt-1 font-mono text-xs">{item.reason}</p>
                        </div>
                        <div className="font-mono text-xs font-bold">
                          Akcja: {item.action} · Dowód: {item.evidence_strength}
                        </div>
                      </div>
                      {item.human_review_required && (
                        <p className="mt-3 flex items-center gap-2 font-mono text-xs font-bold text-amber-800">
                          <AlertTriangle className="h-4 w-4" /> Wymaga jawnego przeglądu człowieka
                        </p>
                      )}
                      <div className="mt-3 font-mono text-xs">
                        <strong>EvidencePermission:</strong>{' '}
                        {permissions.length === 0
                          ? 'brak'
                          : permissions
                              .map(
                                (permission) =>
                                  `${permission.claim_code} / ${permission.allowed_operation}`
                              )
                              .join(', ')}
                      </div>
                      <fieldset className="mt-4">
                        <legend className="font-mono text-xs font-bold">Decyzja użytkownika</legend>
                        <div className="mt-2 grid gap-2 md:grid-cols-3">
                          {DECISION_OPTIONS.map((option) => {
                            const id = `${item.source_reference}-${option.value}`;
                            return (
                              <label
                                key={option.value}
                                htmlFor={id}
                                className="cursor-pointer border border-black p-3 font-mono text-xs focus-within:ring-2 focus-within:ring-blue-700"
                              >
                                <input
                                  id={id}
                                  type="radio"
                                  name={`decision-${item.source_reference}`}
                                  value={option.value}
                                  checked={decisions[item.source_reference] === option.value}
                                  disabled={loading || saving}
                                  onChange={() => {
                                    setDecisions((current) => ({
                                      ...current,
                                      [item.source_reference]: option.value,
                                    }));
                                    setValidationError(null);
                                  }}
                                  className="mr-2"
                                />
                                <strong>{option.label}</strong>
                                <span className="mt-1 block text-[10px]">{option.description}</span>
                              </label>
                            );
                          })}
                        </div>
                      </fieldset>
                    </article>
                  );
                })}
              </section>
            );
          })
        )}

        <section className="border-2 border-black bg-white p-6">
          <h2 className="flex items-center gap-2 font-serif text-2xl font-bold uppercase">
            <ShieldCheck className="h-6 w-6" /> Uniwersalne guardraile
          </h2>
          <ul className="mt-4 grid gap-2 font-mono text-xs md:grid-cols-2">
            {plan.prohibited_claims.map((claim) => (
              <li key={claim.code} className="border border-black p-3">
                <strong>{claim.code}</strong>
                <span className="mt-1 block">{claim.reason}</span>
              </li>
            ))}
          </ul>
          <label className="mt-5 flex items-start gap-3 border-2 border-black bg-amber-50 p-4 font-mono text-sm focus-within:ring-2 focus-within:ring-blue-700">
            <input
              type="checkbox"
              checked={guardrailsAcknowledged}
              disabled={loading || saving}
              onChange={(event) => {
                setGuardrailsAcknowledged(event.target.checked);
                setValidationError(null);
              }}
              className="mt-1"
            />
            Rozumiem, że generator nie może tworzyć twierdzeń bez odpowiedniego dowodu.
          </label>
        </section>

        <footer className="sticky bottom-0 flex flex-wrap gap-3 border-2 border-black bg-white p-4 shadow-[0_-3px_0_0_#000]">
          <Button
            variant="outline"
            disabled={loading || saving || stale || Boolean(error)}
            onClick={() => void save(false)}
          >
            {saving ? 'Zapisywanie…' : 'Zapisz wersję roboczą'}
          </Button>
          <Button disabled={!canApprove} onClick={() => void save(true)}>
            {saving ? 'Zapisywanie…' : 'Zatwierdź plan'}
          </Button>
          <Button variant="outline" onClick={() => router.push(`/resumes/${resumeId}/positioning`)}>
            Wróć do Career Positioning
          </Button>
        </footer>
      </div>
    </main>
  );
}
