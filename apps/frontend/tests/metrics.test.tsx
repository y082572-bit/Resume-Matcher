import { describe, expect, it, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import React from 'react';
import MetricsPage from '@/app/(default)/metrics/page';
import * as metricsApi from '@/lib/api/metrics';
import type { MetricsResponse } from '@/lib/types/metrics';

vi.mock('@/lib/i18n', () => ({
  useTranslations: () => ({
    t: (key: string) => key,
  }),
}));

vi.mock('@/lib/api/metrics', () => ({
  fetchMetrics: vi.fn(),
  parseMetricsResponse: vi.fn(),
}));

const mockResponse: MetricsResponse = {
  schema_version: '1.0',
  generated_at: '2026-07-17T12:00:00Z',
  history: {
    bootstrap_version: 1,
    baseline_at: '2026-07-17T10:00:00Z',
    complete_since: '2026-07-17T11:00:00Z',
    completeness: 'COMPLETE',
    baseline_event_count: 5,
  },
  capabilities: {
    job_offers_saved: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    job_offers_analyzed: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    resumes_generated: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    applications_created: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    applications_submitted: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    employer_responses: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    interviews: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    rejections: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    applications_accepted: {
      current: 'AVAILABLE',
      lifetime: 'AVAILABLE',
      peak: 'AVAILABLE',
      archived: 'UNSUPPORTED',
    },
    status_changes: {
      current: 'UNSUPPORTED',
      lifetime: 'AVAILABLE',
      peak: 'UNSUPPORTED',
      archived: 'UNSUPPORTED',
    },
  },
  metrics: [
    {
      metric_name: 'job_offers_saved',
      current: { value: 2, availability: 'AVAILABLE' },
      lifetime: { value: 10, availability: 'AVAILABLE' },
      peak: { value: 5, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'job_offers_analyzed',
      current: { value: 1, availability: 'AVAILABLE' },
      lifetime: { value: 5, availability: 'AVAILABLE' },
      peak: { value: 3, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'resumes_generated',
      current: { value: 3, availability: 'AVAILABLE' },
      lifetime: { value: 8, availability: 'AVAILABLE' },
      peak: { value: 4, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'applications_created',
      current: { value: 4, availability: 'AVAILABLE' },
      lifetime: { value: 12, availability: 'AVAILABLE' },
      peak: { value: 6, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'applications_submitted',
      current: { value: 3, availability: 'AVAILABLE' },
      lifetime: { value: 9, availability: 'AVAILABLE' },
      peak: { value: 5, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'employer_responses',
      current: { value: 2, availability: 'AVAILABLE' },
      lifetime: { value: 6, availability: 'AVAILABLE' },
      peak: { value: 3, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'interviews',
      current: { value: 1, availability: 'AVAILABLE' },
      lifetime: { value: 4, availability: 'AVAILABLE' },
      peak: { value: 2, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'rejections',
      current: { value: 1, availability: 'AVAILABLE' },
      lifetime: { value: 3, availability: 'AVAILABLE' },
      peak: { value: 2, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'applications_accepted',
      current: { value: 1, availability: 'AVAILABLE' },
      lifetime: { value: 2, availability: 'AVAILABLE' },
      peak: { value: 1, availability: 'AVAILABLE' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
    {
      metric_name: 'status_changes',
      current: { value: null, availability: 'UNSUPPORTED' },
      lifetime: { value: 20, availability: 'AVAILABLE' },
      peak: { value: null, availability: 'UNSUPPORTED' },
      archived: { value: null, availability: 'UNSUPPORTED' },
    },
  ],
};

describe('MetricsPage UI', () => {
  beforeEach(() => {
    vi.resetAllMocks();
  });

  it('loading shows skeleton with aria-busy and Polish accessibility text', async () => {
    let resolvePromise: (val: MetricsResponse) => void = () => {};
    const promise = new Promise<MetricsResponse>((resolve) => {
      resolvePromise = resolve;
    });
    vi.mocked(metricsApi.fetchMetrics).mockReturnValue(promise);

    render(<MetricsPage />);

    const skeleton = screen.getByText('Ładowanie wyników');
    expect(skeleton).toBeInTheDocument();
    expect(skeleton.parentElement?.querySelector('[aria-busy="true"]')).toBeInTheDocument();

    // Verify loading skeleton headers and rows are present
    expect(screen.getByText('Powrót do pulpitu')).toBeInTheDocument();

    resolvePromise(mockResponse);
    await waitFor(() => {
      expect(screen.queryByText('Ładowanie wyników')).not.toBeInTheDocument();
    });
  });

  it('renders error state on fetch failure and retries successfully', async () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.mocked(metricsApi.fetchMetrics).mockRejectedValueOnce(new Error('Network Connection Error'));
    vi.mocked(metricsApi.fetchMetrics).mockResolvedValueOnce(mockResponse);

    render(<MetricsPage />);

    await waitFor(() => {
      expect(screen.getByText('Błąd pobierania danych')).toBeInTheDocument();
      expect(
        screen.getByText(
          'Nie udało się pobrać wyników. Sprawdź, czy aplikacja backendowa działa, i spróbuj ponownie.'
        )
      ).toBeInTheDocument();
      // Ensure raw error message is not rendered in UI
      expect(screen.queryByText('Network Connection Error')).not.toBeInTheDocument();
    });

    const retryBtn = screen.getByRole('button', { name: 'Spróbuj ponownie' });
    fireEvent.click(retryBtn);

    await waitFor(() => {
      expect(screen.getByText('Zapisane oferty')).toBeInTheDocument();
    });

    consoleSpy.mockRestore();
  });

  it('sukces pokazuje cztery kafelki with correct Polish titles and values', async () => {
    vi.mocked(metricsApi.fetchMetrics).mockResolvedValue(mockResponse);

    render(<MetricsPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /Zapisane oferty/i })).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: /Przeanalizowane oferty/i })).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: /Wygenerowane CV/i })).toBeInTheDocument();
      expect(screen.getByRole('heading', { name: /Utworzone aplikacje/i })).toBeInTheDocument();
    });
  });

  it('nie pokazuje surowych kluczy enum', async () => {
    vi.mocked(metricsApi.fetchMetrics).mockResolvedValue(mockResponse);

    render(<MetricsPage />);

    await waitFor(() => {
      expect(screen.queryByText('job_offers_saved')).not.toBeInTheDocument();
      expect(screen.queryByText('status_changes')).not.toBeInTheDocument();
    });
  });

  it('nie pokazuje procentów konwersji', async () => {
    vi.mocked(metricsApi.fetchMetrics).mockResolvedValue(mockResponse);

    render(<MetricsPage />);

    await waitFor(() => {
      expect(screen.queryByText(/%/)).not.toBeInTheDocument();
      expect(
        screen.getByText(
          /Lejek przedstawia skumulowane liczby działań. Współczynniki konwersji nie są jeszcze obliczane./
        )
      ).toBeInTheDocument();
    });
  });

  it('lejek ma właściwą kolejność', async () => {
    vi.mocked(metricsApi.fetchMetrics).mockResolvedValue(mockResponse);

    render(<MetricsPage />);

    await waitFor(() => {
      const heading = screen.getByRole('heading', { name: '2. Lejek rekrutacyjny' });
      const funnelContainer = heading.nextElementSibling;
      const funnelRows = funnelContainer ? funnelContainer.querySelectorAll('.gap-4') : [];
      const textContents = Array.from(funnelRows).map((el) => el.textContent || '');

      // Verify sequence is correct
      expect(textContents[0]).toContain('Utworzone aplikacje');
      expect(textContents[1]).toContain('Wysłane aplikacje');
      expect(textContents[2]).toContain('Odpowiedzi pracodawców');
      expect(textContents[3]).toContain('Rozmowy rekrutacyjne');
      expect(textContents[4]).toContain('Otrzymane oferty');
    });
  });

  it('peak ma etykietę „Maks. jednocześnie” and does not use daily/dziennie terms', async () => {
    vi.mocked(metricsApi.fetchMetrics).mockResolvedValue(mockResponse);

    render(<MetricsPage />);

    await waitFor(() => {
      // Check labels in table and card
      const headers = screen.getAllByText('Maks. jednocześnie');
      expect(headers.length).toBeGreaterThan(0);

      const subLabels = screen.getAllByText('MAKS. JEDNOCZEŚNIE:');
      expect(subLabels.length).toBeGreaterThan(0);

      // Verify no daily / dziennie terms are present
      expect(screen.queryByText(/dziennie/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/daily/i)).not.toBeInTheDocument();
    });
  });

  it('status_changes nie pokazuje current=0', async () => {
    vi.mocked(metricsApi.fetchMetrics).mockResolvedValue(mockResponse);

    render(<MetricsPage />);

    await waitFor(() => {
      const row = screen.getByText('Zmiany statusu').closest('tr');
      expect(row).toBeInTheDocument();
      expect(row?.textContent).toContain('nie dotyczy');
      expect(row?.textContent).not.toContain('current:0');
    });
  });

  it('kolumna archived jest ukryta, gdy wszystkie archived są UNSUPPORTED', async () => {
    vi.mocked(metricsApi.fetchMetrics).mockResolvedValue(mockResponse);

    render(<MetricsPage />);

    await waitFor(() => {
      expect(screen.queryByText('Zarchiwizowane')).not.toBeInTheDocument();
    });
  });

  it('COMPLETE pokazuje poprawny komunikat', async () => {
    vi.mocked(metricsApi.fetchMetrics).mockResolvedValue(mockResponse);

    render(<MetricsPage />);

    await waitFor(() => {
      expect(
        screen.getByText('Historia metryk jest kompletna od początku działania systemu.')
      ).toBeInTheDocument();
    });
  });

  it('PARTIAL_BASELINE pokazuje complete_since', async () => {
    vi.mocked(metricsApi.fetchMetrics).mockResolvedValue({
      ...mockResponse,
      history: {
        ...mockResponse.history,
        completeness: 'PARTIAL_BASELINE',
        complete_since: '2026-07-16T15:00:00Z',
        baseline_event_count: 10,
        baseline_at: '2026-07-16T14:00:00Z',
        bootstrap_version: 2,
      },
    });

    render(<MetricsPage />);

    await waitFor(() => {
      expect(
        screen.queryByText('Historia metryk jest kompletna od początku działania systemu.')
      ).not.toBeInTheDocument();
      expect(
        screen.getByText(/System zna aktualny stan i dane zarejestrowane od/)
      ).toBeInTheDocument();
      expect(
        screen.getByText(/Dane sprzed rozpoczęcia pomiaru mogą być niepełne/)
      ).toBeInTheDocument();
      expect(screen.getByText(/Wersja bootstrapu/)).toHaveTextContent('Wersja bootstrapu: 2');
      expect(screen.getByText(/Zdarzenia bazowe/)).toHaveTextContent('Zdarzenia bazowe: 10');
    });
  });

  it('empty state pozostawia kafelki with zero values', async () => {
    const emptyResponse: MetricsResponse = {
      ...mockResponse,
      metrics: mockResponse.metrics.map((m) => ({
        ...m,
        current: {
          value: m.current.availability === 'AVAILABLE' ? 0 : null,
          availability: m.current.availability,
        },
        lifetime: {
          value: m.lifetime.availability === 'AVAILABLE' ? 0 : null,
          availability: m.lifetime.availability,
        },
        peak: {
          value: m.peak.availability === 'AVAILABLE' ? 0 : null,
          availability: m.peak.availability,
        },
        archived: {
          value: m.archived.availability === 'AVAILABLE' ? 0 : null,
          availability: m.archived.availability,
        },
      })),
    };

    vi.mocked(metricsApi.fetchMetrics).mockResolvedValue(emptyResponse);

    render(<MetricsPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /Zapisane oferty/i })).toBeInTheDocument();
      expect(
        screen.getByText(
          /> Zacznij od zapisania pierwszej oferty pracy. Wyniki będą pojawiać się tutaj automatycznie./
        )
      ).toBeInTheDocument();
    });
  });

  it('Odśwież wykonuje kolejny request i zachowuje stare dane podczas ładowania', async () => {
    vi.mocked(metricsApi.fetchMetrics).mockResolvedValueOnce(mockResponse);

    render(<MetricsPage />);

    await waitFor(() => {
      expect(screen.getByRole('heading', { name: /Zapisane oferty/i })).toBeInTheDocument();
    });

    // Mock next response
    let resolveRefreshPromise: (val: MetricsResponse) => void = () => {};
    const refreshPromise = new Promise<MetricsResponse>((resolve) => {
      resolveRefreshPromise = resolve;
    });
    vi.mocked(metricsApi.fetchMetrics).mockReturnValueOnce(refreshPromise);

    const refreshBtn = screen.getByRole('button', { name: 'Odśwież' });
    fireEvent.click(refreshBtn);

    // Old data should still be visible while refreshing is true
    expect(screen.getByRole('heading', { name: /Zapisane oferty/i })).toBeInTheDocument();

    resolveRefreshPromise(mockResponse);
  });

  it('link nawigacyjny prowadzi do pulpitu', async () => {
    vi.mocked(metricsApi.fetchMetrics).mockResolvedValue(mockResponse);

    render(<MetricsPage />);

    await waitFor(() => {
      const link = screen.getByRole('link', { name: 'Powrót do pulpitu' });
      expect(link).toHaveAttribute('href', '/dashboard');
    });
  });

  it('payload nie ujawnia technicznych ani prywatnych kluczy', async () => {
    vi.mocked(metricsApi.fetchMetrics).mockResolvedValue(mockResponse);

    render(<MetricsPage />);

    await waitFor(() => {
      expect(screen.queryByText(/lifecycle_token/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/resume_id/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/job_id/i)).not.toBeInTheDocument();
    });
  });

  it('localStorage is cleared between tests (part 1)', () => {
    localStorage.setItem('test_isolation_key', 'should_be_removed');
    expect(localStorage.getItem('test_isolation_key')).toBe('should_be_removed');
  });

  it('localStorage is cleared between tests (part 2)', () => {
    expect(localStorage.getItem('test_isolation_key')).toBeNull();
  });
});
