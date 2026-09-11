import assert from 'node:assert/strict';
import test from 'node:test';
import { api, ApiError, changeRequestSession } from '../src/api.ts';

function response(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
}

function captureSessionEvents(t) {
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window');
  const events = [];
  Object.defineProperty(globalThis, 'window', {
    configurable: true,
    value: { dispatchEvent: (event) => events.push(event.type) },
  });
  t.after(() => {
    if (previous) Object.defineProperty(globalThis, 'window', previous);
    else delete globalThis.window;
  });
  return events;
}

test('GET pagination is explicit: requesting users returns one page without hidden follow-up reads', async (t) => {
  changeRequestSession(false);
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (...args) => {
    calls.push(args);
    return response({ items: [{ id: '1' }], total: 2000 });
  });
  const result = await api('/users');
  assert.deepEqual(result, { items: [{ id: '1' }], total: 2000 });
  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], '/api/users');
  assert.equal(calls[0][1].credentials, 'include');
});

test('the optional signal reaches fetch and rejects after cancellation during body parsing', async (t) => {
  changeRequestSession(false);
  const controller = new AbortController();
  let finishBody;
  t.mock.method(globalThis, 'fetch', async (_path, options) => {
    assert.equal(options.signal, controller.signal);
    return {
      status: 200,
      ok: true,
      json: () =>
        new Promise((resolve) => {
          finishBody = resolve;
        }),
    };
  });
  const loading = api('/channels', 'GET', undefined, controller.signal);
  await Promise.resolve();
  controller.abort();
  finishBody({ items: ['stale'] });
  await assert.rejects(loading, { name: 'AbortError' });
});

test('a late 401 from an old session cannot expire the next user session', async (t) => {
  changeRequestSession(false);
  const events = captureSessionEvents(t);
  let finish;
  t.mock.method(
    globalThis,
    'fetch',
    () =>
      new Promise((resolve) => {
        finish = resolve;
      }),
  );
  const oldRequest = api('/users');
  changeRequestSession(true);
  finish(response({ detail: 'expired old session' }, 401));
  await assert.rejects(oldRequest, { name: 'AbortError' });
  assert.deepEqual(events, []);
});

test('current session 401 errors still expire the session and preserve API error detail', async (t) => {
  changeRequestSession(false);
  const events = captureSessionEvents(t);
  t.mock.method(globalThis, 'fetch', async () =>
    response({ detail: { message: 'Sign in again', reason: 'expired' } }, 401),
  );
  await assert.rejects(api('/users'), (error) => {
    assert.ok(error instanceof ApiError);
    assert.equal(error.message, 'Sign in again');
    assert.equal(error.status, 401);
    assert.equal(error.detail.reason, 'expired');
    return true;
  });
  assert.deepEqual(events, ['session-expired']);
});

test('previous-session responses cannot overwrite the current CSRF token; logout clears it', async (t) => {
  changeRequestSession(false);
  let finishOld;
  const postedTokens = [];
  t.mock.method(globalThis, 'fetch', async (path, options) => {
    if (path === '/api/slow')
      return new Promise((resolve) => {
        finishOld = resolve;
      });
    if (path === '/api/auth/login') return response({ csrf_token: 'new-session-token' });
    postedTokens.push(options.headers['X-CSRF-Token']);
    return new Response(null, { status: 204 });
  });
  const oldRequest = api('/slow');
  await api('/auth/login', 'POST', { username: 'test' });
  changeRequestSession(true);
  finishOld(response({ csrf_token: 'old-session-token' }));
  await assert.rejects(oldRequest, { name: 'AbortError' });
  await api('/action', 'POST');
  changeRequestSession(false);
  await api('/action', 'POST');
  assert.deepEqual(postedTokens, ['new-session-token', '']);
});

test('existing JSON and FormData calls retain their body/header behavior', async (t) => {
  changeRequestSession(false);
  const calls = [];
  t.mock.method(globalThis, 'fetch', async (_path, options) => {
    calls.push(options);
    return response({ saved: true });
  });
  assert.deepEqual(await api('/records', 'POST', { amount: '25.00' }), { saved: true });
  const form = new FormData();
  form.set('name', 'channel');
  await api('/uploads', 'POST', form);
  assert.equal(calls[0].headers['Content-Type'], 'application/json');
  assert.equal(calls[0].body, '{"amount":"25.00"}');
  assert.equal(calls[1].body, form);
  assert.equal(calls[1].headers['Content-Type'], undefined);
});
