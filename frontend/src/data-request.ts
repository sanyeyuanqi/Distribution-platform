export type DataSnapshot<T> = { data: T | null; error: string; loading: boolean };
export type DataUpdate<T> = T | null | ((previous: T | null) => T | null);

// One resource belongs to one path and one authenticated session. It has no shared cache.
export function createDataRequest<T>(request: ((signal: AbortSignal) => Promise<T>) | null) {
  let snapshot: DataSnapshot<T> = { data: null, error: '', loading: request !== null };
  let controller: AbortController | null = null;
  const listeners = new Set<() => void>();
  const publish = (next: DataSnapshot<T>) => {
    snapshot = next;
    listeners.forEach((listener) => listener());
  };
  const cancel = () => {
    controller?.abort();
    controller = null;
  };
  return {
    getSnapshot: () => snapshot,
    subscribe: (listener: () => void) => {
      listeners.add(listener);
      return () => {
        listeners.delete(listener);
      };
    },
    cancel,
    load: async () => {
      if (!request) return;
      cancel();
      const current = new AbortController();
      controller = current;
      publish({ ...snapshot, error: '', loading: true });
      try {
        const data = await request(current.signal);
        if (controller === current && !current.signal.aborted) publish({ data, error: '', loading: false });
      } catch (reason) {
        if (controller === current && !current.signal.aborted)
          publish({
            ...snapshot,
            error: reason instanceof Error ? reason.message : String(reason),
            loading: false,
          });
      } finally {
        if (controller === current) controller = null;
      }
    },
    setData: (update: DataUpdate<T>) => {
      if (!request) return;
      // A confirmed mutation is newer than an already-running read.
      cancel();
      publish({
        data:
          typeof update === 'function' ? (update as (previous: T | null) => T | null)(snapshot.data) : update,
        error: '',
        loading: false,
      });
    },
  };
}

// Refresh local API data while its page is visible; this never starts remote work.
export function startDataPolling(
  resource: Pick<ReturnType<typeof createDataRequest>, 'load' | 'getSnapshot'>,
  intervalMs: number,
  page: Pick<Document, 'visibilityState' | 'addEventListener' | 'removeEventListener'> = document,
  timers: Pick<Window, 'setInterval' | 'clearInterval'> = window,
) {
  if (!Number.isFinite(intervalMs) || intervalMs <= 0) return () => {};
  let stopped = false;
  const refresh = () => {
    if (!stopped && page.visibilityState === 'visible' && !resource.getSnapshot().loading)
      void resource.load();
  };
  const timer = timers.setInterval(refresh, intervalMs);
  page.addEventListener('visibilitychange', refresh);
  return () => {
    stopped = true;
    timers.clearInterval(timer);
    page.removeEventListener('visibilitychange', refresh);
  };
}
