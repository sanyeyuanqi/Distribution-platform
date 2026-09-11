import { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { Bell, LoaderCircle } from 'lucide-react';
import { api, ApiError, datetime, Loading, Modal, useApp } from '../core';
import './announcement-notifications.css';

type Announcement = {
  id: string;
  version: number;
  status: string;
  audience: string[];
  title_zh: string;
  title_en?: string;
  content_zh: string;
  content_en?: string;
  published_at?: string;
  updated_at?: string;
  unread?: boolean;
  dismissed_today?: boolean;
};
type Snapshot = { route: string; today: string; items: Announcement[] };
const itemKey = (item: Announcement) => `${item.id}:${item.version}`;
const isDay = (day: unknown): day is string => typeof day === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(day);

export default function AnnouncementNotifications({
  onUnreadChange,
  openRequest,
}: {
  onUnreadChange: (count: number) => void;
  openRequest: number;
}) {
  const { user, t, lang, notify } = useApp();
  const location = useLocation();
  const route = `${location.key}:${location.pathname}${location.search}${location.hash}`;
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [closed, setClosed] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ key: string; message: string } | null>(null);
  const [occupied, setOccupied] = useState(false);
  const [manualRoute, setManualRoute] = useState<string | null>(null);
  const [manualIndex, setManualIndex] = useState(0);
  const [fetchStatus, setFetchStatus] = useState<'loading' | 'ready' | 'error'>('loading');
  const fetchStatusRef = useRef(fetchStatus);
  fetchStatusRef.current = fetchStatus;
  const openedRequest = useRef(openRequest);
  const suppressLoadingResult = useRef<string | null>(null);
  const controller = useRef<AbortController | null>(null);
  const sequence = useRef(0);
  const busyRef = useRef(false);
  const day = useRef('');
  const alive = useRef(true);
  const current = useRef({ route, owner: user!.id });
  current.current = { route, owner: user!.id };

  const refresh = useCallback(async () => {
    controller.current?.abort();
    const request = ++sequence.current;
    if (document.hidden) return;
    const abort = new AbortController();
    controller.current = abort;
    setFetchStatus('loading');
    try {
      const response = await fetch('/api/announcements', { credentials: 'include', signal: abort.signal });
      if (response.status === 401) window.dispatchEvent(new Event('session-expired'));
      if (!response.ok) throw new Error('Announcement request failed');
      const result = await response.json();
      if (
        !alive.current ||
        abort.signal.aborted ||
        sequence.current !== request ||
        current.current.route !== route
      )
        return;
      if (!isDay(result.today) || result.timezone !== 'Asia/Shanghai' || !Array.isArray(result.items))
        throw new Error('Invalid announcement response');
      const items = (result.items as Announcement[]).filter(
        (item) =>
          item.status === 'published' &&
          Array.isArray(item.audience) &&
          item.audience.includes(user!.role) &&
          typeof item.id === 'string' &&
          Number.isInteger(item.version) &&
          item.version > 0,
      );
      if (day.current && day.current !== result.today) setClosed(new Set());
      if (suppressLoadingResult.current !== null) {
        if (!suppressLoadingResult.current || suppressLoadingResult.current === result.today)
          setClosed((previous) => new Set([...previous, ...items.map(itemKey)]));
        suppressLoadingResult.current = null;
      }
      day.current = result.today;
      setSnapshot({ route, today: result.today, items });
      setFetchStatus('ready');
      onUnreadChange(items.filter((item) => item.unread).length);
    } catch {
      if (
        alive.current &&
        !abort.signal.aborted &&
        sequence.current === request &&
        current.current.route === route
      ) {
        // A stale list must not reappear after a route change or a failed availability check.
        setSnapshot(null);
        setFetchStatus('error');
      }
    }
  }, [route, user!.role, onUnreadChange]);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      controller.current?.abort();
      sequence.current++;
    };
  }, []);

  useEffect(() => {
    setSnapshot(null);
    setClosed(new Set());
    setError(null);
    setManualRoute(null);
    suppressLoadingResult.current = null;
    void refresh();
    return () => {
      controller.current?.abort();
      sequence.current++;
    };
  }, [refresh]);

  useEffect(() => {
    if (openedRequest.current === openRequest) return;
    openedRequest.current = openRequest;
    suppressLoadingResult.current = null;
    setManualRoute(route);
    setManualIndex(0);
    setSnapshot(null);
    setError(null);
    void refresh();
  }, [openRequest, route, refresh]);

  useEffect(() => {
    const changed = () => void refresh();
    const visibility = () => {
      if (document.hidden) {
        controller.current?.abort();
        sequence.current++;
        setSnapshot(null);
      } else void refresh();
    };
    let midnight: ReturnType<typeof setTimeout>;
    const scheduleMidnight = () => {
      const localNow = Date.now() + 8 * 60 * 60 * 1000;
      const nextDay = Math.floor(localNow / 86400000) * 86400000 + 86400000;
      midnight = setTimeout(
        () => {
          void refresh();
          scheduleMidnight();
        },
        nextDay - localNow + 200,
      );
    };
    scheduleMidnight();
    window.addEventListener('focus', changed);
    window.addEventListener('announcements-updated', changed);
    document.addEventListener('visibilitychange', visibility);
    return () => {
      clearTimeout(midnight);
      window.removeEventListener('focus', changed);
      window.removeEventListener('announcements-updated', changed);
      document.removeEventListener('visibilitychange', visibility);
    };
  }, [refresh]);

  useEffect(() => {
    const check = () =>
      setOccupied(
        [...document.querySelectorAll('[role="dialog"]')].some(
          (element) => !element.closest('.announcement-reminder'),
        ),
      );
    const observer = new MutationObserver(check);
    observer.observe(document.body, { childList: true, subtree: true });
    check();
    return () => observer.disconnect();
  }, []);

  const visible = snapshot?.route === route ? snapshot.items : [];
  const currentRound = useRef(visible);
  currentRound.current = visible;
  const pending = visible.filter((item) => !item.dismissed_today && !closed.has(itemKey(item)));
  const manualOpen = manualRoute === route;
  const manualOpenRef = useRef(manualOpen);
  manualOpenRef.current = manualOpen;
  const activeIndex = Math.min(manualIndex, Math.max(0, visible.length - 1));
  const active = manualOpen ? visible[activeIndex] : pending[0];
  const closeRound = useCallback(() => {
    // Closing the loading dialog must also close the pending result for this visit.
    if (manualOpenRef.current && !currentRound.current.length && fetchStatusRef.current === 'loading')
      suppressLoadingResult.current = day.current;
    setClosed((previous) => new Set([...previous, ...currentRound.current.map(itemKey)]));
    setError(null);
    setManualRoute(null);
  }, []);

  const dismissToday = async () => {
    if (!active || !snapshot || busyRef.current) return;
    const target = active;
    const key = itemKey(target);
    const context = { ...current.current };
    const viewRequest = openedRequest.current;
    busyRef.current = true;
    setBusy(true);
    setError(null);
    try {
      const result = await api(`/announcements/${encodeURIComponent(target.id)}/dismiss-today`, 'POST', {
        version: target.version,
      });
      if (!alive.current || current.current.owner !== context.owner) return;
      if (result?.ok !== true || result.version !== target.version || !isDay(result.dismissed_on))
        throw new Error(
          t('未能确认今日关闭，请重试。', 'The dismissal could not be confirmed. Please retry.'),
        );
      if (current.current.route === context.route) {
        setSnapshot((previous) =>
          previous?.route === context.route && previous.today === result.dismissed_on
            ? {
                ...previous,
                items: previous.items.map((item) =>
                  itemKey(item) === key ? { ...item, dismissed_today: true } : item,
                ),
              }
            : previous,
        );
        if (manualOpen && viewRequest === openedRequest.current) closeRound();
      }
      window.dispatchEvent(new Event('announcements-updated'));
    } catch (reason) {
      if (
        alive.current &&
        current.current.owner === context.owner &&
        current.current.route === context.route
      ) {
        setError({
          key,
          message:
            reason instanceof Error
              ? reason.message
              : t('今日关闭失败，请重试。', 'Could not dismiss for today. Please retry.'),
        });
        if (reason instanceof ApiError && [404, 409].includes(reason.status)) {
          notify(reason.message, true);
          void refresh();
        }
      }
    } finally {
      busyRef.current = false;
      if (alive.current) setBusy(false);
    }
  };

  if (
    (!manualOpen && !active) ||
    occupied ||
    (!manualOpen && user!.role === 'superadmin' && location.pathname.replace(/\/+$/, '') === '/announcements')
  )
    return null;
  return (
    <div
      className="announcement-reminder"
      data-announcement-id={active?.id}
      data-announcement-version={active?.version}
    >
      <Modal
        key={active ? itemKey(active) : fetchStatus}
        title={manualOpen ? t('已发布公告', 'Published announcements') : t('公告提醒', 'Announcement')}
        subtitle={
          manualOpen
            ? t('查看当前发布的公告。', 'View currently published announcements.')
            : t('普通关闭后，切换页面仍会提醒。', 'Closing only pauses reminders until you change pages.')
        }
        onClose={closeRound}
        footer={
          <>
            {manualOpen && visible.length > 1 && (
              <div className="announcement-reminder-pagination">
                <button
                  type="button"
                  className="text-button"
                  disabled={busy || activeIndex === 0}
                  onClick={() => {
                    setManualIndex(activeIndex - 1);
                    setError(null);
                  }}
                >
                  {t('上一条', 'Previous')}
                </button>
                <span>
                  {activeIndex + 1} / {visible.length}
                </span>
                <button
                  type="button"
                  className="text-button"
                  disabled={busy || activeIndex === visible.length - 1}
                  onClick={() => {
                    setManualIndex(activeIndex + 1);
                    setError(null);
                  }}
                >
                  {t('下一条', 'Next')}
                </button>
              </div>
            )}
            {active && (
              <span className="announcement-reminder-day">
                {t('今日关闭按北京时间计算', 'Daily dismissal follows Beijing time')}
                {!manualOpen && pending.length > 1
                  ? t(` · 还有 ${pending.length - 1} 条`, ` · ${pending.length - 1} more`)
                  : ''}
              </span>
            )}
            <div className="actions">
              <button type="button" className="button secondary" onClick={closeRound}>
                {t('关闭', 'Close')}
              </button>
              {active && (
                <button
                  type="button"
                  className="button"
                  disabled={busy || active.dismissed_today}
                  onClick={() => void dismissToday()}
                >
                  {busy ? <LoaderCircle size={15} className="spin" /> : <Bell size={15} />}
                  {busy
                    ? t('正在保存…', 'Saving…')
                    : active.dismissed_today
                      ? t('今日已关闭', 'Dismissed today')
                      : t('今日关闭', 'Dismiss for today')}
                </button>
              )}
            </div>
          </>
        }
      >
        {active ? (
          <article className="announcement-reminder-content">
            <h3>{t(active.title_zh, active.title_en || active.title_zh)}</h3>
            {(active.published_at || active.updated_at) && (
              <p className="announcement-reminder-date">
                {datetime(active.published_at || active.updated_at)}
              </p>
            )}
            <div className="prose-text">{t(active.content_zh, active.content_en || active.content_zh)}</div>
            {lang === 'en' && !active.content_en && (
              <p className="muted">English unavailable · Showing Chinese</p>
            )}
          </article>
        ) : fetchStatus === 'error' ? (
          <div className="announcement-reminder-error" role="alert">
            <p>{t('读取公告失败，请重试。', 'Could not load announcements. Please retry.')}</p>
            <button type="button" className="button" onClick={() => void refresh()}>
              {t('重试', 'Retry')}
            </button>
          </div>
        ) : fetchStatus === 'loading' ? (
          <Loading />
        ) : (
          <p className="muted">{t('暂无已发布公告', 'No published announcements')}</p>
        )}
        {active && error?.key === itemKey(active) && (
          <div className="announcement-reminder-error" role="alert">
            {error.message}
          </div>
        )}
      </Modal>
    </div>
  );
}
