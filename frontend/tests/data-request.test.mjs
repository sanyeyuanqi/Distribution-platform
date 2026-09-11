import assert from 'node:assert/strict';
import test from 'node:test';
import { createDataRequest, startDataPolling } from '../src/data-request.ts';

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

function pollingEnvironment() {
  const listeners = new Set();
  let tick;
  const page = {
    visibilityState: 'visible',
    addEventListener: (_type, callback) => listeners.add(callback),
    removeEventListener: (_type, callback) => listeners.delete(callback),
  };
  const timers = {
    setInterval: (callback, interval) => {
      assert.equal(interval, 30000);
      tick = callback;
      return 1;
    },
    clearInterval: (id) => assert.equal(id, 1),
  };
  return {
    page,
    timers,
    tick: () => tick(),
    visibility: (value) => {
      page.visibilityState = value;
      listeners.forEach((callback) => callback());
    },
    listeners,
  };
}

test('background page reads pause while hidden and never overlap or abort an active read', async () => {
  const pending = [];
  const resource = createDataRequest((signal) => {
    const request = deferred();
    pending.push({ ...request, signal });
    return request.promise;
  });
  const env = pollingEnvironment();
  const initial = resource.load();
  const stop = startDataPolling(resource, 30000, env.page, env.timers);
  env.tick();
  assert.equal(pending.length, 1);
  assert.equal(pending[0].signal.aborted, false);
  pending[0].resolve({ amount: '100' });
  await initial;
  env.visibility('hidden');
  env.tick();
  assert.equal(pending.length, 1);
  env.visibility('visible');
  assert.equal(pending.length, 2);
  env.tick();
  assert.equal(pending.length, 2);
  assert.equal(pending[1].signal.aborted, false);
  assert.equal(resource.getSnapshot().data.amount, '100');
  pending[1].resolve({ amount: '125' });
  await pending[1].promise;
  assert.equal(resource.getSnapshot().data.amount, '125');
  stop();
});

test('stopping page polling detaches listeners and ignores already queued timer callbacks', async () => {
  let reads = 0;
  const resource = createDataRequest(async () => ++reads);
  await resource.load();
  const env = pollingEnvironment();
  const stop = startDataPolling(resource, 30000, env.page, env.timers);
  stop();
  assert.equal(env.listeners.size, 0);
  env.tick();
  env.visibility('visible');
  assert.equal(reads, 1);
});

test('refresh cancels the previous fetch and ignores a late response even if the transport ignores abort', async () => {
  const pending = [];
  const resource = createDataRequest((signal) => {
    const operation = deferred();
    pending.push({ ...operation, signal });
    return operation.promise;
  });
  const first = resource.load();
  const second = resource.load();
  assert.equal(pending[0].signal.aborted, true);
  pending[1].resolve({ amount: '25.00' });
  await second;
  pending[0].resolve({ amount: '10.00' });
  await first;
  assert.deepEqual(resource.getSnapshot(), { data: { amount: '25.00' }, error: '', loading: false });
});

test('an older failure cannot replace the active response or end its loading state', async () => {
  const pending = [];
  const resource = createDataRequest(() => {
    const operation = deferred();
    pending.push(operation);
    return operation.promise;
  });
  const first = resource.load();
  const second = resource.load();
  pending[0].reject(new Error('outdated failure'));
  await first;
  assert.deepEqual(resource.getSnapshot(), { data: null, error: '', loading: true });
  pending[1].resolve(['current']);
  await second;
  assert.deepEqual(resource.getSnapshot(), { data: ['current'], error: '', loading: false });
});

test('unmount cancellation prevents a late result from notifying listeners', async () => {
  const pending = deferred();
  let signal;
  const resource = createDataRequest((requestSignal) => {
    signal = requestSignal;
    return pending.promise;
  });
  let notifications = 0;
  resource.subscribe(() => notifications++);
  const loaded = resource.load();
  const notificationsBeforeCancel = notifications;
  resource.cancel();
  assert.equal(signal.aborted, true);
  pending.resolve({ id: 'old-user' });
  await loaded;
  assert.equal(notifications, notificationsBeforeCancel);
  assert.equal(resource.getSnapshot().data, null);
});

test('new path/session resources start empty and a disabled path never exposes previous data', async () => {
  const first = createDataRequest(async () => ({ payee: 'first', amount: '80' }));
  await first.load();
  const next = createDataRequest(async () => ({ payee: 'second', amount: '10' }));
  assert.deepEqual(next.getSnapshot(), { data: null, error: '', loading: true });
  const disabled = createDataRequest(null);
  disabled.setData(first.getSnapshot().data);
  await disabled.load();
  assert.deepEqual(disabled.getSnapshot(), { data: null, error: '', loading: false });
  await next.load();
  assert.equal(next.getSnapshot().data.payee, 'second');
});

test('confirmed local updates cancel older reads and support functional and null setters', async () => {
  const pending = deferred();
  const resource = createDataRequest(() => pending.promise);
  const loaded = resource.load();
  resource.setData({ amount: '125' });
  resource.setData((previous) => ({ ...previous, settled: true }));
  pending.resolve({ amount: '100' });
  await loaded;
  assert.deepEqual(resource.getSnapshot(), {
    data: { amount: '125', settled: true },
    error: '',
    loading: false,
  });
  resource.setData(null);
  assert.deepEqual(resource.getSnapshot(), { data: null, error: '', loading: false });
});

test('refresh preserves same-scope data while loading and exposes current failures', async () => {
  let fail = false;
  const resource = createDataRequest(async () => {
    if (fail) throw new Error('temporarily unavailable');
    return { amount: '25' };
  });
  await resource.load();
  fail = true;
  const refresh = resource.load();
  assert.deepEqual(resource.getSnapshot(), { data: { amount: '25' }, error: '', loading: true });
  await refresh;
  assert.deepEqual(resource.getSnapshot(), {
    data: { amount: '25' },
    error: 'temporarily unavailable',
    loading: false,
  });
});
