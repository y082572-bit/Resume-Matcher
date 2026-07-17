import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { API_BASE, apiFetch, apiPost, getUploadUrl } from '@/lib/api/client';

/**
 * The single backend client. Tests cover URL resolution, JSON POST shape, and
 * the timeout → friendly-message behavior (240s matches the backend wait_for).
 * `fetch` is stubbed so nothing hits the network.
 */

describe('api client', () => {
  let fetchMock: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    fetchMock = vi.fn().mockResolvedValue(new Response('{}', { status: 200 }));
    vi.stubGlobal('fetch', fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  describe('API_BASE / getUploadUrl', () => {
    it('defaults to /api/v1 in the browser env', () => {
      expect(API_BASE).toBe('/api/v1');
      expect(getUploadUrl()).toBe('/api/v1/resumes/upload');
    });
  });

  describe('apiFetch URL resolution', () => {
    it('prefixes a relative endpoint with API_BASE and passes an abort signal', async () => {
      await apiFetch('/health');
      expect(fetchMock).toHaveBeenCalledWith(
        '/api/v1/health',
        expect.objectContaining({ signal: expect.anything() })
      );
    });

    it('adds a leading slash to a bare endpoint', async () => {
      await apiFetch('health');
      expect(fetchMock.mock.calls[0][0]).toBe('/api/v1/health');
    });

    it('passes absolute URLs through untouched', async () => {
      await apiFetch('https://example.com/x');
      expect(fetchMock.mock.calls[0][0]).toBe('https://example.com/x');
    });
  });

  describe('apiPost', () => {
    it('sends a JSON body with POST + Content-Type', async () => {
      await apiPost('/jobs/upload', { job_descriptions: ['x'] });
      const [url, init] = fetchMock.mock.calls[0];
      expect(url).toBe('/api/v1/jobs/upload');
      expect(init.method).toBe('POST');
      expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json');
      expect(init.body).toBe(JSON.stringify({ job_descriptions: ['x'] }));
    });
  });

  describe('timeout / error handling', () => {
    it('maps an AbortError to a friendly timeout message', async () => {
      const abortErr = new Error('aborted');
      abortErr.name = 'AbortError';
      fetchMock.mockRejectedValueOnce(abortErr);
      await expect(apiFetch('/slow')).rejects.toThrow(/timed out/i);
    });

    it('rethrows non-abort errors unchanged', async () => {
      fetchMock.mockRejectedValueOnce(new Error('network boom'));
      await expect(apiFetch('/x')).rejects.toThrow('network boom');
    });

    it('aborts after the timeout and reports a timeout error', async () => {
      vi.useFakeTimers();
      try {
        fetchMock.mockImplementation(
          (_url: string, init: RequestInit) =>
            new Promise((_resolve, reject) => {
              init.signal?.addEventListener('abort', () => {
                const e = new Error('The operation was aborted');
                e.name = 'AbortError';
                reject(e);
              });
            })
        );
        const promise = apiFetch('/slow', undefined, 5000);
        const expectation = expect(promise).rejects.toThrow(/timed out/i);
        await vi.advanceTimersByTimeAsync(5000);
        await expectation;
      } finally {
        vi.useRealTimers();
      }
    });

    it('aborts fetch with external signal and is not classified as timeout', async () => {
      const extController = new AbortController();
      const abortErr = new Error('The user aborted a request.');
      abortErr.name = 'AbortError';

      fetchMock.mockImplementation((_url: string, init: RequestInit) => {
        return new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () => {
            reject(abortErr);
          });
        });
      });

      const promise = apiFetch('/some-endpoint', { signal: extController.signal });
      extController.abort();

      await expect(promise).rejects.toThrow('The user aborted a request.');
    });

    it('timeout still triggers if external signal is not aborted', async () => {
      vi.useFakeTimers();
      const extController = new AbortController();
      try {
        fetchMock.mockImplementation(
          (_url: string, init: RequestInit) =>
            new Promise((_resolve, reject) => {
              init.signal?.addEventListener('abort', () => {
                const e = new Error('The operation was aborted');
                e.name = 'AbortError';
                reject(e);
              });
            })
        );
        const promise = apiFetch('/slow', { signal: extController.signal }, 5000);
        const expectation = expect(promise).rejects.toThrow(/timed out/i);
        await vi.advanceTimersByTimeAsync(5000);
        await expectation;
      } finally {
        vi.useRealTimers();
      }
    });

    it('removes abort listener from external signal in finally', async () => {
      const extController = new AbortController();
      const addSpy = vi.spyOn(extController.signal, 'addEventListener');
      const removeSpy = vi.spyOn(extController.signal, 'removeEventListener');

      await apiFetch('/fast', { signal: extController.signal });

      expect(addSpy).toHaveBeenCalled();
      expect(removeSpy).toHaveBeenCalled();
    });

    it('works without external signal without regression', async () => {
      await apiFetch('/health');
      expect(fetchMock).toHaveBeenCalled();
    });

    it('aborts request immediately if signal is already aborted', async () => {
      const extController = new AbortController();
      extController.abort();
      const promise = apiFetch('/fast', { signal: extController.signal });
      await expect(promise).rejects.toThrow('The user aborted a request.');
      expect(fetchMock).not.toHaveBeenCalled();
    });
  });
});
