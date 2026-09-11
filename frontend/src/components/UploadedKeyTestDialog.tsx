import { useCallback, useEffect, useRef, useState } from 'react';
import { LoaderCircle, Play, RefreshCw } from 'lucide-react';
import { api, ApiError, Field, Modal, nonce, Notice, Status, useApp } from '../core';
import type { Row } from '../core';
import Select from './Select';
import './uploaded-key-test.css';
import { sortModelsByReleaseDate } from '../model-release-order';

const runningStates = ['pending', 'queued', 'running'];
const testModels = (value: unknown): string[] =>
  Array.isArray(value)
    ? [...new Set(value.filter((model): model is string => typeof model === 'string' && !!model))]
    : [];
const count = (value: unknown) => (Number.isInteger(value) && Number(value) >= 0 ? Number(value) : 0);
type Request = {
  action: 'test';
  test_scope: 'channel';
  idempotency_key: string;
  test_all?: true;
  model?: string;
};

export default function UploadedKeyTestDialog({
  channel,
  onChannelUpdated,
  onClose,
  onSubmitted,
}: {
  channel: Row;
  onChannelUpdated?: (channel: Row) => void;
  onClose: () => void;
  onSubmitted: (taskId: string) => void;
}) {
  const { t } = useApp();
  const channelId = channel.id;
  const initial = channel.connectivity_test || {};
  const [models, setModels] = useState<string[]>(() => testModels(channel.local_test_models));
  const [model, setModel] = useState(models.includes(initial.model) ? initial.model : models[0] || '');
  const [result, setResult] = useState<Row>(initial);
  const [taskId, setTaskId] = useState<string>(initial.task_id || '');
  const activeTask = useRef<string>(initial.task_id || '');
  const [available, setAvailable] = useState(!channel.archived && channel.local_test_available === true);
  const [unavailableReason, setUnavailableReason] = useState(channel.local_test_reason || '');
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const [reading, setReading] = useState(false);
  const readLock = useRef(false);
  const [error, setError] = useState('');
  const [readError, setReadError] = useState('');
  const [uncertain, setUncertain] = useState<Request | null>(null);
  const uncertainRequest = useRef<Request | null>(null);
  const [differentTask, setDifferentTask] = useState(false);
  const requestKey = useRef(nonce());
  const alive = useRef(true);
  const revision = useRef(0);
  const latest = useRef({ t, onClose, onSubmitted, onChannelUpdated });
  latest.current = { t, onClose, onSubmitted, onChannelUpdated };
  const running = !!taskId && runningStates.includes(result.status);
  const locked = busy || running || !!uncertain;

  const refresh = useCallback(async () => {
    if (readLock.current) return;
    readLock.current = true;
    const requestedTask = activeTask.current;
    const requestedRevision = revision.current;
    setReading(true);
    try {
      const channel = await api(`/channels/${channelId}`);
      if (!alive.current || requestedRevision !== revision.current) return;
      if (channel.id !== channelId || !Array.isArray(channel.local_test_models))
        throw new Error(
          latest.current.t(
            '渠道测试信息无法确认，请稍后刷新。',
            'Could not confirm channel test details. Refresh shortly.',
          ),
        );
      setAvailable(!channel.archived && channel.local_test_available === true);
      setUnavailableReason(channel.local_test_reason || '');
      const next = channel.connectivity_test || {};
      if (!submitting.current && !uncertainRequest.current && !runningStates.includes(next.status)) {
        const nextModels = testModels(channel.local_test_models);
        setModels(nextModels);
        setModel((selected: string) => (nextModels.includes(selected) ? selected : nextModels[0] || ''));
      }
      if (requestedTask && next.task_id !== requestedTask) {
        // Never attribute another task's observations to the task shown in this window.
        setDifferentTask(!!next.task_id && runningStates.includes(next.status));
        setReadError(
          latest.current.t(
            '当前测试结果尚未同步，请稍后刷新；其他任务的结果不会覆盖此窗口。',
            'This test result is not available yet. Refresh shortly; results from another task will not replace it.',
          ),
        );
        return;
      }
      if (!requestedTask && next.task_id && uncertainRequest.current) {
        setReadError(
          latest.current.t(
            '请确认原提交请求，避免将其他测试结果归入本次请求。',
            'Confirm the original submission before associating a test result with it.',
          ),
        );
        return;
      }
      if (!requestedTask && next.task_id) {
        activeTask.current = next.task_id;
        setTaskId(next.task_id);
      }
      setDifferentTask(false);
      if (next.task_id) setResult(next);
      latest.current.onChannelUpdated?.({
        ...channel,
        local_test_available: !channel.archived && channel.local_test_available === true,
      });
      setReadError('');
    } catch (reason) {
      if (alive.current && requestedRevision === revision.current)
        setReadError(
          reason instanceof Error
            ? reason.message
            : latest.current.t('暂时无法读取测试结果，请重试。', 'Could not load results. Retry.'),
        );
    } finally {
      readLock.current = false;
      if (alive.current) setReading(false);
    }
  }, [channelId]);

  useEffect(() => {
    alive.current = true;
    void refresh();
    return () => {
      alive.current = false;
      revision.current++;
    };
  }, [refresh]);
  useEffect(() => {
    if (!running) return;
    const timer = setInterval(() => void refresh(), 2500);
    return () => clearInterval(timer);
  }, [running, refresh]);
  const close = useCallback(() => {
    if (!submitting.current) latest.current.onClose();
  }, []);
  const submit = async (all: boolean, retry?: Request) => {
    if (
      submitting.current ||
      running ||
      (!retry &&
        (!available || differentTask || uncertain || !models.length || (!all && !models.includes(model))))
    )
      return;
    const body: Request = retry || {
      action: 'test',
      test_scope: 'channel',
      idempotency_key: requestKey.current,
      ...(all ? { test_all: true as const } : { model }),
    };
    submitting.current = true;
    setBusy(true);
    setError('');
    revision.current++;
    try {
      const task = await api(`/channels/${channelId}/actions`, 'POST', body);
      if (!task?.id || task.kind !== 'test')
        throw new Error(
          t(
            '测试请求未确认，请确认提交结果后再开始新的测试。',
            'The test request was not confirmed. Confirm its result before starting another test.',
          ),
        );
      if (!alive.current) return;
      const selected = body.test_all ? models : [body.model!];
      activeTask.current = task.id;
      setTaskId(task.id);
      setResult({
        task_id: task.id,
        status: 'pending',
        test_all: !!body.test_all,
        model: body.model,
        total_count: selected.length,
        completed_count: 0,
        current_model: null,
        model_results: selected.map((name) => ({
          model: name,
          status: 'pending',
          tested_count: 0,
          passed_count: 0,
          failed_count: 0,
        })),
      });
      setUncertain(null);
      uncertainRequest.current = null;
      setDifferentTask(false);
      setReadError('');
      requestKey.current = nonce();
      latest.current.onSubmitted(task.id);
      void refresh();
    } catch (reason) {
      if (!alive.current) return;
      setError(
        reason instanceof TypeError
          ? t(
              '连接中断，提交结果尚未确认。请确认同一请求的结果，不会自动重复测试。',
              'Connection interrupted; submission is unconfirmed. Confirm the same request; tests are not retried automatically.',
            )
          : reason instanceof Error
            ? reason.message
            : t('测试提交失败，请重试。', 'Could not submit the test. Retry.'),
      );
      if (!(reason instanceof ApiError) || reason.status >= 500) {
        setUncertain(body);
        uncertainRequest.current = body;
      } else {
        setUncertain(null);
        uncertainRequest.current = null;
        requestKey.current = nonce();
      }
    } finally {
      submitting.current = false;
      if (alive.current) setBusy(false);
    }
  };
  const hasModelResults = Array.isArray(result.model_results) && result.model_results.length > 0;
  const legacySingle =
    !hasModelResults && typeof result.model === 'string' && result.model && !result.test_all;
  const modelResults: Row[] = hasModelResults ? result.model_results : legacySingle ? [result] : [];
  const rows = modelResults.flatMap<Row>((row) => {
    const keys = Array.isArray(row.key_results)
      ? row.key_results.filter((key: Row) => Number.isInteger(key.key_index) && key.key_index > 0)
      : [];
    return keys.map((key: Row) => ({ ...key, model: row.model }));
  });
  const total = rows.length ? count(result.total_key_count) || rows.length : 0;
  const completed = Math.min(count(result.completed_key_count), total);
  const passed = count(result.passed_key_count);
  const failed = count(result.failed_key_count);
  const legacySummary = !!taskId && !running && modelResults.length > 0 && rows.length === 0;
  const blocked = locked || !available || differentTask || !models.length;

  return (
    <Modal
      title={t('本地测试已上传 Key', 'Test uploaded keys locally')}
      className="uploaded-key-test-modal"
      onClose={close}
      footer={
        <>
          <span className="muted">
            {t(
              '关闭窗口后，已提交的测试仍会在后台继续。',
              'Submitted tests continue in the background after closing.',
            )}
          </span>
          <button className="button secondary" disabled={busy} onClick={close}>
            {t('关闭', 'Close')}
          </button>
        </>
      }
    >
      <div className="uploaded-key-test">
        {error && (
          <div role="alert">
            <Notice kind="error">{error}</Notice>
          </div>
        )}
        {!available && !running && (
          <Notice>
            {unavailableReason ||
              t('仅可测试已确认上传的 Key。', 'Only confirmed uploaded keys can be tested.')}
          </Notice>
        )}
        <div className="uploaded-key-test-controls">
          <Field label={t('测试模型', 'Test model')}>
            <Select
              value={model}
              aria-label={t('测试模型', 'Test model')}
              disabled={locked}
              onChange={(event) => {
                setModel(event.target.value);
                requestKey.current = nonce();
                setError('');
              }}
            >
              {sortModelsByReleaseDate(models).map((name) => (
                <option key={name} value={name}>
                  {name}
                </option>
              ))}
            </Select>
          </Field>
          <div className="uploaded-key-test-actions">
            <button
              className="button"
              disabled={blocked || !models.includes(model)}
              onClick={() => void submit(false)}
            >
              <Play size={15} />
              {t('测试该模型', 'Test this model')}
            </button>
            <button className="button secondary" disabled={blocked} onClick={() => void submit(true)}>
              <Play size={15} />
              {t(`测试全部模型 (${models.length})`, `Test all models (${models.length})`)}
            </button>
            {uncertain && (
              <button
                className="button secondary"
                disabled={busy || running}
                onClick={() => void submit(!!uncertain.test_all, uncertain)}
              >
                {busy ? <LoaderCircle size={15} className="spin" /> : <RefreshCw size={15} />}
                {t('确认提交结果', 'Confirm submission')}
              </button>
            )}
          </div>
        </div>
        <section
          className="uploaded-key-test-results"
          aria-label={t('测试进度与结果', 'Test progress and results')}
        >
          <div className="uploaded-key-test-progress-heading">
            <strong>
              {t('测试进度', 'Test progress')}
              {total > 0 && ` ${completed} / ${total}`}
            </strong>
            <button className="text-button" disabled={reading || busy} onClick={() => void refresh()}>
              <RefreshCw size={14} className={reading ? 'spin' : ''} />
              {t('刷新结果', 'Refresh results')}
            </button>
          </div>
          {total > 0 && (
            <progress aria-label={t('已完成测试项', 'Completed tests')} value={completed} max={total} />
          )}
          <div className="uploaded-key-test-summary" aria-live="polite">
            {taskId && <Status value={result.status || 'pending'} />}
            {total > 0 && (
              <>
                <span className="uploaded-key-test-pass">{t(`通过 ${passed}`, `Passed ${passed}`)}</span>
                <span className="uploaded-key-test-fail">{t(`失败 ${failed}`, `Failed ${failed}`)}</span>
                <span>{t(`共 ${total} 项`, `${total} tests`)}</span>
              </>
            )}
            {running && result.current_model && (
              <span className="wrap-cell">
                {t('正在测试', 'Testing')}: {result.current_model}
                {count(result.current_key_index) > 0 && ` · Key ${result.current_key_index}`}
              </span>
            )}
          </div>
          {legacySummary && (
            <p className="uploaded-key-test-history muted">
              {t(
                '旧测试未保存逐 Key 明细，请重新测试。',
                'This older test has no per-key details. Please run a new test.',
              )}
            </p>
          )}
          {readError && (
            <div role="alert">
              <Notice kind="error">{readError}</Notice>
            </div>
          )}
          <div
            className="uploaded-key-test-table"
            tabIndex={0}
            role="region"
            aria-label={t('逐 Key 测试结果', 'Per-key test results')}
          >
            <table>
              <thead>
                <tr>
                  <th>{t('模型', 'Model')}</th>
                  <th>{t('结果', 'Result')}</th>
                  <th>{t('耗时', 'Duration')}</th>
                  <th>{t('状态码', 'Status code')}</th>
                  <th>{t('信息', 'Information')}</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row, index) => {
                  const providerMessage =
                    typeof row.provider_message === 'string' ? row.provider_message.trim() : '';
                  return (
                    <tr key={`${row.model}-${row.key_index}-${index}`} data-key-index={row.key_index}>
                      <td>
                        {row.model || '—'}
                        <small>Key {row.key_index}</small>
                      </td>
                      <td>
                        <Status value={row.status === 'succeeded' ? 'passed' : row.status || 'not_tested'} />
                      </td>
                      <td>
                        {typeof row.latency_ms === 'number' &&
                        Number.isFinite(row.latency_ms) &&
                        row.latency_ms >= 0
                          ? `${(row.latency_ms / 1000).toFixed(2)} s`
                          : '—'}
                      </td>
                      <td>{row.http_status ?? '—'}</td>
                      <td className="uploaded-key-test-information">
                        {(providerMessage || row.message) && (
                          <span className="uploaded-key-test-message">{providerMessage || row.message}</span>
                        )}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
            {!rows.length && !legacySummary && (
              <p className="muted">
                {running
                  ? t('正在准备逐 Key 测试结果，请稍候。', 'Preparing per-key results. Please wait.')
                  : t(
                      '选择模型并开始测试，结果将在这里显示。',
                      'Choose a model and start a test to see results here.',
                    )}
              </p>
            )}
          </div>
        </section>
      </div>
    </Modal>
  );
}
