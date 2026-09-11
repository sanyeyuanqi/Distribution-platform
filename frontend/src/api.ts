type ErrorDetail = Record<string, any>;

let csrf = '';
let sessionRevision = 0;

// Responses from a previous login must not update the new session's token or expire it.
export function changeRequestSession(signedIn: boolean) {
  sessionRevision += 1;
  if (!signedIn) csrf = '';
}

export class ApiError extends Error {
  status: number;
  detail?: ErrorDetail;

  constructor(message: string, status: number, detail?: ErrorDetail) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

export async function api<T = any>(
  path: string,
  method = 'GET',
  data?: unknown,
  signal?: AbortSignal,
): Promise<T> {
  const requestSession = sessionRevision;
  const headers: Record<string, string> = {};
  if (method !== 'GET') headers['X-CSRF-Token'] = csrf;
  if (data !== undefined && !(data instanceof FormData)) headers['Content-Type'] = 'application/json';
  const res = await fetch(`/api${path}`, {
    method,
    credentials: 'include',
    headers,
    body: data === undefined ? undefined : data instanceof FormData ? data : JSON.stringify(data),
    signal,
  });
  const result = res.status === 204 ? null : await res.json().catch(() => null);
  signal?.throwIfAborted();
  if (requestSession !== sessionRevision) throw new DOMException('Session changed', 'AbortError');
  if (!res.ok) {
    if (res.status === 401 && path !== '/auth/login' && path !== '/auth/me')
      window.dispatchEvent(new Event('session-expired'));
    const detail = result?.detail;
    throw new ApiError(
      typeof detail === 'string'
        ? detail
        : Array.isArray(detail)
          ? detail
              .map((e: ErrorDetail) => {
                const field = e.loc?.slice(1).join('.');
                const labels: Record<string, string> = { password: '密码', current_password: '当前密码' };
                return `${labels[field] || field}：${e.msg}`;
              })
              .join('；')
          : typeof detail?.message === 'string'
            ? detail.message
            : detail
              ? JSON.stringify(detail)
              : `HTTP ${res.status}`,
      res.status,
      detail && typeof detail === 'object' && !Array.isArray(detail) ? detail : undefined,
    );
  }
  if (result?.csrf_token) csrf = result.csrf_token;
  return result;
}
