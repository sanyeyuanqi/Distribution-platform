import { useEffect, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { ArrowDown, RefreshCw } from 'lucide-react';
import { useApp } from '../core';
import './pull-to-refresh.css';

type Props = {
  children: ReactNode;
  className?: string;
  'aria-label'?: string;
  onRefresh: () => void;
  refreshing: boolean;
  disabled?: boolean;
};

type Gesture = { identifier: number; x: number; y: number; vertical: boolean; distance: number };
const THRESHOLD = 64;
const MAX_DISTANCE = 96;
const INTERACTIVE =
  'button, a, input, select, textarea, [contenteditable]:not([contenteditable="false"]), [role="button"], [role="link"], [role="combobox"], [role="slider"], [role="checkbox"], [role="radio"], [data-pull-refresh-ignore]';

/** Owns the scroll surface; the parent supplies the loading cycle and refresh action. */
export default function PullToRefresh({
  children,
  className = '',
  'aria-label': ariaLabel,
  onRefresh,
  refreshing,
  disabled = false,
}: Props) {
  const { t } = useApp();
  const container = useRef<HTMLDivElement>(null);
  const gesture = useRef<Gesture | null>(null);
  const locked = useRef(false);
  const sawRefreshing = useRef(false);
  const current = useRef({ onRefresh, refreshing, disabled });
  current.current = { onRefresh, refreshing, disabled };
  const [distance, setDistance] = useState(0);
  const [waiting, setWaiting] = useState(false);

  useEffect(() => {
    if (refreshing) {
      if (locked.current) sawRefreshing.current = true;
      gesture.current = null;
      setDistance(0);
    } else if (locked.current && sawRefreshing.current) {
      locked.current = false;
      sawRefreshing.current = false;
      setWaiting(false);
      setDistance(0);
    }
  }, [refreshing]);

  useEffect(() => {
    if (disabled) {
      gesture.current = null;
      setDistance(0);
    }
  }, [disabled]);

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const mobile = window.matchMedia('(max-width: 760px)');
    const reset = () => {
      gesture.current = null;
      setDistance(0);
    };
    const blocked = () =>
      !mobile.matches || current.current.disabled || current.current.refreshing || locked.current;
    const canStartAt = (target: EventTarget | null) => {
      if (!(target instanceof Element) || target.closest(INTERACTIVE)) return false;
      for (let node = target; node && node !== element; node = node.parentElement!) {
        const style = getComputedStyle(node);
        if (/auto|scroll/.test(style.overflowY) && node.scrollHeight > node.clientHeight + 1) return false;
      }
      return true;
    };
    const start = (event: TouchEvent) => {
      reset();
      if (blocked() || event.defaultPrevented || event.touches.length !== 1 || element.scrollTop > 1) return;
      if (!canStartAt(event.target)) return;
      const touch = event.touches[0];
      gesture.current = {
        identifier: touch.identifier,
        x: touch.clientX,
        y: touch.clientY,
        vertical: false,
        distance: 0,
      };
    };
    const move = (event: TouchEvent) => {
      const active = gesture.current;
      if (!active) return;
      if (blocked() || event.defaultPrevented || event.touches.length !== 1 || element.scrollTop > 1) {
        reset();
        return;
      }
      const touch = event.touches[0];
      if (touch.identifier !== active.identifier) {
        reset();
        return;
      }
      const dx = Math.abs(touch.clientX - active.x);
      const dy = touch.clientY - active.y;
      if (!active.vertical) {
        if (Math.max(dx, Math.abs(dy)) < 8) return;
        if (dy <= 0 || dx >= dy) {
          reset();
          return;
        }
        active.vertical = true;
      }
      if (dy <= 0 || !event.cancelable) {
        reset();
        return;
      }
      event.preventDefault();
      active.distance = Math.min(MAX_DISTANCE, dy * 0.5);
      setDistance(active.distance);
    };
    const end = (event: TouchEvent) => {
      const active = gesture.current;
      reset();
      if (
        !active ||
        !active.vertical ||
        active.distance < THRESHOLD ||
        blocked() ||
        event.touches.length ||
        element.scrollTop > 1
      )
        return;
      // onRefresh only schedules loading. Lock before it runs, including the false → true gap.
      locked.current = true;
      sawRefreshing.current = false;
      setWaiting(true);
      try {
        current.current.onRefresh();
      } catch (reason) {
        locked.current = false;
        sawRefreshing.current = false;
        setWaiting(false);
        throw reason;
      }
    };
    const scroll = () => {
      if (gesture.current && element.scrollTop > 1) reset();
    };
    const cancel = () => reset();
    const options: AddEventListenerOptions = { passive: false };
    element.addEventListener('touchstart', start, options);
    element.addEventListener('touchmove', move, options);
    element.addEventListener('touchend', end, options);
    element.addEventListener('touchcancel', cancel, options);
    element.addEventListener('scroll', scroll, { passive: true });
    mobile.addEventListener('change', cancel);
    window.addEventListener('blur', cancel);
    return () => {
      element.removeEventListener('touchstart', start);
      element.removeEventListener('touchmove', move);
      element.removeEventListener('touchend', end);
      element.removeEventListener('touchcancel', cancel);
      element.removeEventListener('scroll', scroll);
      mobile.removeEventListener('change', cancel);
      window.removeEventListener('blur', cancel);
      gesture.current = null;
    };
  }, []);

  const loading = refreshing || waiting;
  const ready = distance >= THRESHOLD;
  const state = loading ? 'refreshing' : ready ? 'ready' : distance > 0 ? 'pulling' : 'idle';
  return (
    <div
      ref={container}
      className={`pull-to-refresh ${className}`.trim()}
      role="region"
      aria-label={ariaLabel || t('可刷新列表', 'Refreshable list')}
      aria-busy={loading}
      tabIndex={0}
      data-pull-state={state}
    >
      <div
        className="pull-to-refresh-indicator"
        style={{ height: loading ? 52 : distance }}
        aria-hidden={state === 'idle'}
      >
        <div className="pull-to-refresh-status" role="status" aria-live="polite">
          {loading ? <RefreshCw size={16} aria-hidden="true" /> : <ArrowDown size={16} aria-hidden="true" />}
          <span>
            {loading
              ? t('刷新中…', 'Refreshing…')
              : ready
                ? t('松开刷新', 'Release to refresh')
                : t('下拉刷新', 'Pull to refresh')}
          </span>
        </div>
      </div>
      {children}
    </div>
  );
}
