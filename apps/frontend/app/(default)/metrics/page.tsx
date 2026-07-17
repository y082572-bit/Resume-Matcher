'use client';

import React, { useState, useEffect, useCallback } from 'react';
import Link from 'next/link';
import ArrowLeft from 'lucide-react/dist/esm/icons/arrow-left';
import RefreshCw from 'lucide-react/dist/esm/icons/refresh-cw';
import Loader2 from 'lucide-react/dist/esm/icons/loader-2';
import AlertTriangle from 'lucide-react/dist/esm/icons/alert-triangle';
import CheckCircle2 from 'lucide-react/dist/esm/icons/check-circle-2';
import Info from 'lucide-react/dist/esm/icons/info';

import { Card, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { fetchMetrics } from '@/lib/api/metrics';
import { MetricsResponse, MetricType } from '@/lib/types/metrics';

const METRICS_PL_NAMES: Record<MetricType, string> = {
  job_offers_saved: 'Zapisane oferty',
  job_offers_analyzed: 'Przeanalizowane oferty',
  resumes_generated: 'Wygenerowane CV',
  applications_created: 'Utworzone aplikacje',
  applications_submitted: 'Wysłane aplikacje',
  employer_responses: 'Odpowiedzi pracodawców',
  interviews: 'Rozmowy rekrutacyjne',
  rejections: 'Odmowy',
  applications_accepted: 'Otrzymane oferty',
  status_changes: 'Zmiany statusu',
};

const METRICS_PL_DESCRIPTIONS: Record<MetricType, string> = {
  job_offers_saved: 'Liczba ofert pracy zapisanych w systemie.',
  job_offers_analyzed: 'Liczba ofert pracy przeanalizowanych przez silnik AI.',
  resumes_generated: 'Liczba wygenerowanych dedykowanych dokumentów CV.',
  applications_created: 'Liczba rozpoczętych procesów aplikacyjnych.',
  applications_submitted: 'Liczba wysłanych aplikacji (status applied i dalsze).',
  employer_responses: 'Liczba odpowiedzi otrzymanych od pracodawców.',
  interviews: 'Liczba zaproszeń na rozmowy kwalifikacyjne.',
  rejections: 'Liczba otrzymanych odmów.',
  applications_accepted: 'Liczba otrzymanych ofert zatrudnienia.',
  status_changes: 'Liczba zmian statusów w procesie rekrutacyjnym.',
};

const FUNNEL_ORDER: MetricType[] = [
  'applications_created',
  'applications_submitted',
  'employer_responses',
  'interviews',
  'applications_accepted',
];

const RESULTS_ORDER: MetricType[] = [
  'status_changes',
  'rejections',
  'employer_responses',
  'interviews',
  'applications_accepted',
];

export default function MetricsPage() {
  const [data, setData] = useState<MetricsResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [refreshing, setRefreshing] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  const loadData = useCallback(async (isRefresh = false) => {
    if (isRefresh) {
      setRefreshing(true);
    } else {
      setLoading(true);
    }
    setError(null);
    try {
      const response = await fetchMetrics();
      setData(response);
    } catch (err: unknown) {
      console.error('Failed to load metrics:', err);
      setError(
        'Nie udało się pobrać wyników. Sprawdź, czy aplikacja backendowa działa, i spróbuj ponownie.'
      );
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const formatDate = (isoString: string) => {
    if (!isoString) return '';
    try {
      const d = new Date(isoString);
      return d.toLocaleDateString('pl-PL', {
        year: 'numeric',
        month: 'long',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit',
      });
    } catch {
      return isoString;
    }
  };

  const getMetricDimension = (
    type: MetricType,
    dimension: 'current' | 'lifetime' | 'peak' | 'archived'
  ): string | number => {
    if (!data) return 'nie dotyczy';
    const cap = data.capabilities[type];
    if (!cap || cap[dimension] === 'UNSUPPORTED') {
      return 'nie dotyczy';
    }
    const metric = data.metrics.find((m) => m.metric_name === type);
    if (!metric) {
      return 'nie dotyczy';
    }
    const measure = metric[dimension];
    if (measure.availability === 'UNSUPPORTED' || measure.value === null) {
      return 'nie dotyczy';
    }
    return measure.value;
  };

  if (loading) {
    return (
      <main
        className="flex min-h-screen w-full flex-col bg-background px-4 py-6 md:px-8"
        style={{
          backgroundImage:
            'linear-gradient(rgba(29, 78, 216, 0.1) 1px, transparent 1px), linear-gradient(90deg, rgba(29, 78, 216, 0.1) 1px, transparent 1px)',
          backgroundSize: '40px 40px',
        }}
      >
        <div aria-busy="true" className="sr-only">
          Ładowanie wyników
        </div>
        <div className="mx-auto flex w-full max-w-[86rem] flex-1 flex-col">
          <div className="mb-4 inline-flex shrink-0 items-center gap-1 self-start font-mono text-xs uppercase text-ink-soft">
            <ArrowLeft className="h-3.5 w-3.5" />
            Powrót do pulpitu
          </div>

          <div className="flex flex-col border border-black bg-background shadow-sw-lg">
            {/* Header Skeleton */}
            <div className="border-b border-black p-8 md:p-12 shrink-0 bg-background flex flex-col md:flex-row justify-between items-start md:items-center gap-6 animate-pulse">
              <div>
                <div className="h-10 bg-gray-300 w-48 border border-black/10"></div>
                <div className="h-4 bg-gray-200 w-80 mt-3 border border-black/10"></div>
              </div>
              <div className="h-10 bg-gray-200 w-32 border border-black"></div>
            </div>

            {/* Content Skeleton */}
            <div className="p-8 md:p-12 space-y-12">
              {/* Cards Grid */}
              <div>
                <div className="h-4 bg-gray-300 w-48 mb-6 border border-black/10"></div>
                <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
                  {[1, 2, 3, 4].map((i) => (
                    <Card
                      key={i}
                      variant="default"
                      className="border-2 border-black p-6 aspect-square rounded-none bg-canvas animate-pulse flex flex-col justify-between"
                    >
                      <div>
                        <div className="h-3 bg-gray-300 w-1/3 mb-2 border border-black/10"></div>
                        <div className="h-6 bg-gray-300 w-2/3 mb-2 border border-black/10"></div>
                        <div className="h-3 bg-gray-200 w-full mb-1 border border-black/10"></div>
                        <div className="h-3 bg-gray-200 w-3/4 border border-black/10"></div>
                      </div>
                      <div className="h-8 bg-gray-300 w-full border border-black/10"></div>
                    </Card>
                  ))}
                </div>
              </div>

              {/* Funnel */}
              <div>
                <div className="h-4 bg-gray-300 w-36 mb-6 border border-black/10 animate-pulse"></div>
                <div className="border border-black bg-[#F0F0E8] p-6 md:p-8 rounded-none shadow-sw-sm space-y-6">
                  {[1, 2, 3, 4, 5].map((i) => (
                    <div
                      key={i}
                      className="animate-pulse flex flex-col md:flex-row items-center gap-4"
                    >
                      <div className="h-4 bg-gray-300 w-64 border border-black/10"></div>
                      <div className="flex-1 w-full bg-gray-200 border border-black h-8"></div>
                    </div>
                  ))}
                </div>
              </div>

              {/* Table */}
              <div>
                <div className="h-4 bg-gray-300 w-36 mb-6 border border-black/10 animate-pulse"></div>
                <div className="border border-black bg-background overflow-x-auto rounded-none shadow-sw-sm">
                  <table className="w-full border-collapse">
                    <thead>
                      <tr className="bg-[#F0F0E8] border-b border-black">
                        <th className="p-4 border-r border-black">
                          <div className="h-4 bg-gray-300 w-24"></div>
                        </th>
                        <th className="p-4 border-r border-black">
                          <div className="h-4 bg-gray-300 w-12 mx-auto"></div>
                        </th>
                        <th className="p-4 border-r border-black">
                          <div className="h-4 bg-gray-300 w-12 mx-auto"></div>
                        </th>
                        <th className="p-4 border-r border-black">
                          <div className="h-4 bg-gray-300 w-24 mx-auto"></div>
                        </th>
                        <th className="p-4">
                          <div className="h-4 bg-gray-300 w-24 mx-auto"></div>
                        </th>
                      </tr>
                    </thead>
                    <tbody>
                      {[1, 2, 3, 4, 5].map((i) => (
                        <tr key={i} className="border-b border-black last:border-0">
                          <td className="p-4 border-r border-black">
                            <div className="h-4 bg-gray-200 w-32"></div>
                          </td>
                          <td className="p-4 border-r border-black">
                            <div className="h-4 bg-gray-200 w-8 mx-auto"></div>
                          </td>
                          <td className="p-4 border-r border-black">
                            <div className="h-4 bg-gray-200 w-8 mx-auto"></div>
                          </td>
                          <td className="p-4 border-r border-black">
                            <div className="h-4 bg-gray-200 w-8 mx-auto"></div>
                          </td>
                          <td className="p-4">
                            <div className="h-4 bg-gray-200 w-8 mx-auto"></div>
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </div>
            </div>
          </div>
        </div>
      </main>
    );
  }

  if (error) {
    return (
      <main
        className="flex min-h-screen w-full flex-col bg-background px-4 py-6 md:px-8"
        style={{
          backgroundImage:
            'linear-gradient(rgba(29, 78, 216, 0.1) 1px, transparent 1px), linear-gradient(90deg, rgba(29, 78, 216, 0.1) 1px, transparent 1px)',
          backgroundSize: '40px 40px',
        }}
      >
        <div className="mx-auto flex w-full max-w-[86rem] flex-1 flex-col justify-center items-center">
          <div className="border-2 border-red-600 bg-red-50 p-8 shadow-sw-default max-w-lg text-center">
            <AlertTriangle className="mx-auto h-12 w-12 text-red-600 mb-4" />
            <h2 className="font-mono text-lg font-bold text-red-800 uppercase mb-2">
              Błąd pobierania danych
            </h2>
            <p className="text-red-700 text-sm mb-6">{error}</p>
            <Button
              onClick={() => loadData()}
              className="bg-red-600 hover:bg-red-700 text-white rounded-none border border-black shadow-sw-sm"
            >
              Spróbuj ponownie
            </Button>
          </div>
        </div>
      </main>
    );
  }

  if (!data) return null;

  // Check if there is absolutely no activity
  const isDataEmpty = data.metrics.every(
    (m) => (m.lifetime.value ?? 0) === 0 && (m.current.value ?? 0) === 0
  );

  // Get metrics ordered for funnel
  const funnelMetrics = FUNNEL_ORDER.map((type) => {
    return (
      data.metrics.find((m) => m.metric_name === type) || {
        metric_name: type,
        current: { value: null, availability: 'UNSUPPORTED' },
        lifetime: { value: null, availability: 'UNSUPPORTED' },
        peak: { value: null, availability: 'UNSUPPORTED' },
        archived: { value: null, availability: 'UNSUPPORTED' },
      }
    );
  });

  const showArchivedColumn = data.metrics.some(
    (m) => data.capabilities[m.metric_name]?.archived === 'AVAILABLE'
  );

  return (
    <main
      className="flex min-h-screen w-full flex-col bg-background px-4 py-6 md:px-8"
      style={{
        backgroundImage:
          'linear-gradient(rgba(29, 78, 216, 0.1) 1px, transparent 1px), linear-gradient(90deg, rgba(29, 78, 216, 0.1) 1px, transparent 1px)',
        backgroundSize: '40px 40px',
      }}
    >
      <div className="mx-auto flex w-full max-w-[86rem] flex-1 flex-col">
        <Link
          href="/dashboard"
          className="mb-4 inline-flex shrink-0 items-center gap-1 self-start font-mono text-xs uppercase text-ink-soft hover:text-primary"
        >
          <ArrowLeft className="h-3.5 w-3.5" />
          Powrót do pulpitu
        </Link>

        <div className="flex flex-col border border-black bg-background shadow-sw-lg">
          {/* Header */}
          <div className="border-b border-black p-8 md:p-12 shrink-0 bg-background flex flex-col md:flex-row justify-between items-start md:items-center gap-6">
            <div>
              <h1 className="font-serif text-4xl md:text-6xl text-black tracking-tight leading-none uppercase">
                Wyniki
              </h1>
              <p className="mt-3 text-sm font-mono text-blue-700 uppercase tracking-wide font-bold">
                {'// '} Pulpit statystyk i skuteczności procesu poszukiwania pracy
              </p>
            </div>
            <div className="flex flex-col sm:flex-row items-start sm:items-center gap-4">
              <span className="font-mono text-xs text-ink-soft uppercase bg-[#F0F0E8] border border-black px-3 py-1.5 shadow-sw-sm">
                Dane z: {formatDate(data.generated_at)}
              </span>
              <Button
                onClick={() => loadData(true)}
                disabled={refreshing}
                variant="outline"
                className="border-black text-black hover:bg-secondary rounded-none shadow-sw-sm flex items-center gap-2"
              >
                {refreshing ? (
                  <Loader2 className="h-4 w-4 animate-spin" />
                ) : (
                  <RefreshCw className="h-4 w-4" />
                )}
                Odśwież
              </Button>
            </div>
          </div>

          {/* Empty State Overlay */}
          {isDataEmpty && (
            <div className="border-b border-black bg-amber-50 p-6 flex items-center justify-between shadow-inner">
              <div className="flex items-center gap-3">
                <Info className="w-5 h-5 text-amber-800 shrink-0" />
                <p className="font-mono text-sm text-amber-900 font-bold">
                  &gt; Zacznij od zapisania pierwszej oferty pracy. Wyniki będą pojawiać się tutaj
                  automatycznie.
                </p>
              </div>
            </div>
          )}

          {/* Grid Layout of Metrics */}
          <div className="p-8 md:p-12 space-y-12">
            {/* Section 1: Podsumowanie Aktywności */}
            <div>
              <h2 className="font-mono text-xs font-bold uppercase tracking-widest text-blue-700 mb-6">
                1. Podsumowanie aktywności
              </h2>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
                {(
                  [
                    'job_offers_saved',
                    'job_offers_analyzed',
                    'resumes_generated',
                    'applications_created',
                  ] as MetricType[]
                ).map((type) => {
                  const name = METRICS_PL_NAMES[type] || 'Nieznana metryka';
                  const desc = METRICS_PL_DESCRIPTIONS[type] || 'Nieznana metryka';

                  return (
                    <Card
                      key={type}
                      variant="default"
                      className="border-2 border-black flex flex-col justify-between p-6 aspect-square rounded-none bg-canvas hover:translate-y-[-2px] hover:shadow-sw-lg transition-all"
                    >
                      <div>
                        <CardTitle className="text-xl uppercase font-serif text-black mb-2">
                          {name}
                        </CardTitle>
                        <p className="text-xs text-ink-soft opacity-85 leading-relaxed">{desc}</p>
                      </div>
                      <div className="mt-6 pt-4 border-t border-black/10">
                        <div className="flex justify-between items-baseline mb-2">
                          <span className="text-xs font-mono uppercase text-ink-soft">Suma:</span>
                          <span className="text-3xl font-serif font-black text-black">
                            {getMetricDimension(type, 'lifetime')}
                          </span>
                        </div>
                        <div className="flex justify-between text-[11px] font-mono text-ink-soft border-t border-black/5 pt-2">
                          <span>BIEŻĄCE:</span>
                          <span className="font-bold">{getMetricDimension(type, 'current')}</span>
                        </div>
                        <div
                          className="flex justify-between text-[11px] font-mono text-ink-soft mt-1 cursor-help"
                          title="Najwyższa liczba jednocześnie aktywnych elementów zarejestrowana w historii."
                        >
                          <span>MAKS. JEDNOCZEŚNIE:</span>
                          <span className="font-bold">{getMetricDimension(type, 'peak')}</span>
                        </div>
                      </div>
                    </Card>
                  );
                })}
              </div>
            </div>

            {/* Section 2: Lejek rekrutacyjny */}
            <div>
              <h2 className="font-mono text-xs font-bold uppercase tracking-widest text-blue-700 mb-6">
                2. Lejek rekrutacyjny
              </h2>
              <div className="border border-black bg-[#F0F0E8] p-6 md:p-8 rounded-none shadow-sw-sm space-y-6">
                {funnelMetrics.map((m) => {
                  const name = METRICS_PL_NAMES[m.metric_name] || 'Nieznana metryka';
                  const val = getMetricDimension(m.metric_name, 'lifetime');

                  return (
                    <div
                      key={m.metric_name}
                      className="flex flex-col md:flex-row items-start md:items-center gap-4"
                    >
                      {/* Stage Name & Count */}
                      <div className="w-full md:w-64 font-mono text-xs uppercase font-bold text-black flex justify-between shrink-0">
                        <span>{name}</span>
                        <span>{val}</span>
                      </div>

                      {/* Funnel Progress Bar wrapper (equal width, no dynamic styles) */}
                      <div className="flex-1 w-full bg-background border border-black h-8 flex items-center relative overflow-hidden">
                        <div className="bg-blue-700 h-full w-full"></div>
                      </div>
                    </div>
                  );
                })}
                <div className="mt-4 pt-4 border-t border-black/10 font-mono text-[11px] text-ink-soft uppercase leading-relaxed">
                  <p>
                    &gt; Lejek przedstawia skumulowane liczby działań. Współczynniki konwersji nie
                    są jeszcze obliczane.
                  </p>
                  {data.history.completeness === 'PARTIAL_BASELINE' && (
                    <p className="mt-1 text-amber-800 font-bold">
                      &gt; Dane sprzed rozpoczęcia pomiaru mogą być niepełne.
                    </p>
                  )}
                </div>
              </div>
            </div>

            {/* Section 3: Wyniki aplikacji */}
            <div>
              <h2 className="font-mono text-xs font-bold uppercase tracking-widest text-blue-700 mb-6">
                3. Wyniki aplikacji
              </h2>
              <div className="border border-black bg-background overflow-x-auto rounded-none shadow-sw-sm">
                <table className="w-full text-left font-mono text-xs border-collapse">
                  <thead>
                    <tr className="bg-[#F0F0E8] border-b border-black text-black">
                      <th className="p-4 uppercase font-bold border-r border-black">
                        Metryka rekrutacyjna
                      </th>
                      <th className="p-4 uppercase font-bold border-r border-black text-center">
                        Bieżące
                      </th>
                      <th className="p-4 uppercase font-bold border-r border-black text-center">
                        Suma
                      </th>
                      <th
                        className="p-4 uppercase font-bold border-r border-black text-center cursor-help"
                        title="Najwyższa liczba jednocześnie aktywnych elementów zarejestrowana w historii."
                      >
                        Maks. jednocześnie
                      </th>
                      {showArchivedColumn && (
                        <th className="p-4 uppercase font-bold text-center">Zarchiwizowane</th>
                      )}
                    </tr>
                  </thead>
                  <tbody>
                    {RESULTS_ORDER.map((type) => {
                      const name = METRICS_PL_NAMES[type] || 'Nieznana metryka';

                      return (
                        <tr
                          key={type}
                          className="border-b border-black last:border-0 hover:bg-secondary/20"
                        >
                          <td className="p-4 font-bold border-r border-black text-black uppercase">
                            {name}
                          </td>
                          <td className="p-4 border-r border-black text-center text-sm">
                            {getMetricDimension(type, 'current')}
                          </td>
                          <td className="p-4 border-r border-black text-center text-sm font-bold">
                            {getMetricDimension(type, 'lifetime')}
                          </td>
                          <td className="p-4 border-r border-black text-center text-sm">
                            {getMetricDimension(type, 'peak')}
                          </td>
                          {showArchivedColumn && (
                            <td className="p-4 text-center text-sm">
                              {getMetricDimension(type, 'archived')}
                            </td>
                          )}
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
              <div className="mt-2 p-2 font-mono text-[10px] text-ink-soft uppercase">
                * Maks. jednocześnie: Najwyższa liczba jednocześnie aktywnych elementów
                zarejestrowana w historii.
              </div>
            </div>

            {/* Section 4: Kompletność historii */}
            <div>
              <h2 className="font-mono text-xs font-bold uppercase tracking-widest text-blue-700 mb-6">
                4. Kompletność historii
              </h2>
              {data.history.completeness === 'COMPLETE' ? (
                <div className="border-2 border-green-600 bg-green-50/50 p-6 flex flex-col sm:flex-row gap-4 items-start sm:items-center rounded-none shadow-sw-sm">
                  <CheckCircle2 className="h-6 w-6 text-green-700 shrink-0" />
                  <div>
                    <p className="font-mono text-sm font-bold text-green-900 uppercase">
                      Historia metryk jest kompletna od początku działania systemu.
                    </p>
                    <p className="font-mono text-[11px] text-green-700 uppercase mt-1">
                      Wszystkie operacje i dane historyczne są w pełni odzwierciedlone.
                    </p>
                  </div>
                </div>
              ) : (
                <div className="border-2 border-warning bg-amber-50/50 p-6 flex flex-col gap-4 rounded-none shadow-sw-sm">
                  <div className="flex gap-4 items-start sm:items-center">
                    <Info className="h-6 w-6 text-warning shrink-0" />
                    <div>
                      <p className="font-mono text-sm font-bold text-amber-900 uppercase">
                        System zna aktualny stan i dane zarejestrowane od{' '}
                        {formatDate(data.history.complete_since)}. Wcześniejsza historia może być
                        niepełna.
                      </p>
                    </div>
                  </div>
                  <div className="border-t border-warning/20 pt-4 flex flex-wrap gap-x-8 gap-y-2 font-mono text-[10px] uppercase text-amber-800">
                    <div>
                      Wersja bootstrapu:{' '}
                      <span className="font-bold">{data.history.bootstrap_version}</span>
                    </div>
                    <div>
                      Data bazowa:{' '}
                      <span className="font-bold">{formatDate(data.history.baseline_at)}</span>
                    </div>
                    <div>
                      Zdarzenia bazowe:{' '}
                      <span className="font-bold">{data.history.baseline_event_count}</span>
                    </div>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </main>
  );
}
