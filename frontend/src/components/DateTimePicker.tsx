import { useId, useRef, useState } from 'react';
import type { InputHTMLAttributes } from 'react';
import * as Popover from '@radix-ui/react-popover';
import { DayPicker } from '@daypicker/react';
import { enUS, zhCN } from '@daypicker/react/locale';
import { CalendarDays, Check, ChevronLeft, ChevronRight, Clock3, X } from 'lucide-react';
import Select from './Select';
import Tooltip from './Tooltip';
import '@daypicker/react/style.css';
import './datetime-picker.css';

type Props = Omit<
  InputHTMLAttributes<HTMLInputElement>,
  'type' | 'onChange' | 'value' | 'defaultValue' | 'min' | 'max'
> & {
  value: string;
  onChange: (event: { target: { value: string }; currentTarget: { value: string } }) => void;
  locale?: 'zh' | 'en';
  compact?: boolean;
  min?: string;
  max?: string;
};

const pad = (value: number) => String(value).padStart(2, '0');
// These values represent local wall time. Conversion to UTC belongs to the API caller.
function localValue(date: Date) {
  return `${String(date.getFullYear()).padStart(4, '0')}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;
}
function parseLocal(value: string): Date | undefined {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/.test(value)) return;
  const date = new Date(value);
  if (!Number.isNaN(date.getTime()) && localValue(date) === value) return date;
}

export default function DateTimePicker({
  value,
  onChange,
  locale = 'zh',
  compact = false,
  disabled = false,
  readOnly = false,
  required,
  name,
  id,
  form,
  min,
  max,
  className = '',
  placeholder,
  title,
  autoFocus,
  tabIndex,
  'aria-label': ariaLabel,
  'aria-labelledby': labelledBy,
  'aria-describedby': describedBy,
  'aria-invalid': ariaInvalid,
}: Props) {
  const t = (zh: string, en: string) => (locale === 'zh' ? zh : en);
  const headingId = useId();
  const [open, setOpen] = useState(false);
  const [draft, setDraft] = useState('');
  const [month, setMonth] = useState(new Date());
  const [invalid, setInvalid] = useState(false);
  const trigger = useRef<HTMLButtonElement>(null);
  const control = useRef<HTMLInputElement>(null);
  const blocked = disabled || readOnly;
  const picked = parseLocal(draft);
  const lower = min ? parseLocal(min) : undefined;
  const upper = max ? parseLocal(max) : undefined;
  const validDraft = !!picked && (!min || draft >= min) && (!max || draft <= max);
  const firstYear = lower?.getFullYear() ?? Math.min(1900, month.getFullYear());
  const lastYear = upper?.getFullYear() ?? Math.max(new Date().getFullYear() + 100, month.getFullYear());
  const monthNumber = month.getFullYear() * 12 + month.getMonth();
  const label = ariaLabel || placeholder || t('选择日期和时间', 'Select date and time');

  const changeOpen = (next: boolean) => {
    if (next && (blocked || trigger.current?.matches(':disabled'))) return;
    if (next) {
      const initial = parseLocal(value) ? value : localValue(new Date());
      setDraft(initial);
      setMonth(parseLocal(initial)!);
    }
    setOpen(next);
  };
  const commit = (next: string) => {
    if (blocked || trigger.current?.matches(':disabled')) return;
    if (next !== value) onChange({ target: { value: next }, currentTarget: { value: next } });
    setInvalid(false);
    setOpen(false);
  };
  const now = () => {
    const current = new Date();
    setDraft(localValue(current));
    setMonth(current);
  };
  const selectDay = (day: Date) => {
    setDraft(`${localValue(day).slice(0, 10)}T${draft.slice(11)}`);
  };

  return (
    <div className={`datetime-control ${compact ? 'datetime-compact' : ''} ${className}`}>
      <Popover.Root open={open && !blocked} onOpenChange={changeOpen}>
        <Tooltip content={open ? undefined : title} triggerClassName="datetime-tooltip-trigger">
          <Popover.Trigger asChild disabled={blocked} aria-describedby={describedBy}>
            <button
              ref={trigger}
              type="button"
              className="datetime-trigger"
              id={id}
              disabled={blocked}
              autoFocus={autoFocus}
              tabIndex={tabIndex}
              aria-label={ariaLabel}
              aria-labelledby={labelledBy}
              aria-required={required}
              aria-invalid={ariaInvalid || invalid || undefined}
            >
              <span className={value ? '' : 'datetime-placeholder'}>
                {value
                  ? value.replace('T', ' ').replaceAll('-', '/')
                  : placeholder || ariaLabel || t('选择日期和时间', 'Select date and time')}
              </span>
              <CalendarDays size={16} aria-hidden="true" />
            </button>
          </Popover.Trigger>
        </Tooltip>
        <input
          ref={control}
          className="datetime-native"
          type="datetime-local"
          value={value}
          onChange={(event) => commit(event.target.value)}
          required={required}
          disabled={disabled}
          readOnly={readOnly}
          min={min}
          max={max}
          name={name}
          form={form}
          tabIndex={-1}
          aria-hidden="true"
          onInvalid={(event) => {
            event.preventDefault();
            setInvalid(true);
            const input = event.currentTarget;
            const first = input.form
              ? Array.from(input.form.elements).find(
                  (element) =>
                    (element instanceof HTMLInputElement ||
                      element instanceof HTMLSelectElement ||
                      element instanceof HTMLTextAreaElement) &&
                    element.willValidate &&
                    !element.validity.valid,
                )
              : input;
            if (first === input) {
              trigger.current?.focus();
              changeOpen(true);
            } else setOpen(false);
          }}
        />
        <Popover.Portal>
          <Popover.Content
            className="datetime-popover"
            align="end"
            sideOffset={8}
            collisionPadding={12}
            aria-labelledby={headingId}
            onOpenAutoFocus={(event) => {
              event.preventDefault();
              const panel = event.currentTarget as HTMLElement;
              const day = panel.querySelector<HTMLElement>('.rdp-day_button[tabindex="0"]');
              (day || panel.querySelector<HTMLElement>('button'))?.focus();
            }}
            onEscapeKeyDown={(event) => {
              event.preventDefault();
              event.stopPropagation();
              setOpen(false);
            }}
            onInteractOutside={(event) => {
              // Nested hour/month menus are portaled, but still belong to this picker.
              if (event.target instanceof Element && event.target.closest('.select-menu, .app-tooltip'))
                event.preventDefault();
            }}
          >
            <div className="datetime-heading">
              <span id={headingId}>
                <CalendarDays size={16} />
                {label}
              </span>
              <Popover.Close
                type="button"
                className="datetime-icon"
                aria-label={t('关闭日期选择器', 'Close date picker')}
              >
                <X size={16} />
              </Popover.Close>
            </div>
            <div className="datetime-calendar-nav">
              <button
                type="button"
                className="datetime-icon"
                aria-label={t('上个月', 'Previous month')}
                disabled={!!lower && monthNumber <= lower.getFullYear() * 12 + lower.getMonth()}
                onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() - 1, 1))}
              >
                <ChevronLeft size={17} />
              </button>
              <Select
                value={String(month.getFullYear())}
                aria-label={t('年份', 'Year')}
                className="datetime-year"
                onChange={(event) => setMonth(new Date(Number(event.target.value), month.getMonth(), 1))}
              >
                {Array.from({ length: Math.max(1, lastYear - firstYear + 1) }, (_, i) => firstYear + i).map(
                  (year) => (
                    <option key={year} value={year}>
                      {t(`${year}年`, String(year))}
                    </option>
                  ),
                )}
              </Select>
              <Select
                value={String(month.getMonth())}
                aria-label={t('月份', 'Month')}
                className="datetime-month"
                onChange={(event) => setMonth(new Date(month.getFullYear(), Number(event.target.value), 1))}
              >
                {Array.from({ length: 12 }, (_, i) => i).map((index) => (
                  <option
                    key={index}
                    value={index}
                    disabled={
                      (!!lower && month.getFullYear() === lower.getFullYear() && index < lower.getMonth()) ||
                      (!!upper && month.getFullYear() === upper.getFullYear() && index > upper.getMonth())
                    }
                  >
                    {t(
                      `${index + 1}月`,
                      new Intl.DateTimeFormat('en', { month: 'short' }).format(new Date(2000, index, 1)),
                    )}
                  </option>
                ))}
              </Select>
              <button
                type="button"
                className="datetime-icon"
                aria-label={t('下个月', 'Next month')}
                disabled={!!upper && monthNumber >= upper.getFullYear() * 12 + upper.getMonth()}
                onClick={() => setMonth(new Date(month.getFullYear(), month.getMonth() + 1, 1))}
              >
                <ChevronRight size={17} />
              </button>
            </div>
            <div className="datetime-calendar-scroll">
              <DayPicker
                mode="single"
                required
                selected={picked}
                onSelect={selectDay}
                month={month}
                onMonthChange={setMonth}
                hideNavigation
                showOutsideDays
                weekStartsOn={1}
                locale={locale === 'zh' ? zhCN : enUS}
                className="datetime-calendar"
                disabled={[
                  ...(lower
                    ? [{ before: new Date(lower.getFullYear(), lower.getMonth(), lower.getDate()) }]
                    : []),
                  ...(upper
                    ? [{ after: new Date(upper.getFullYear(), upper.getMonth(), upper.getDate()) }]
                    : []),
                ]}
              />
            </div>
            <div className="datetime-time-row">
              <span>
                <Clock3 size={15} />
                {t('时间', 'Time')}
                <small>24h</small>
              </span>
              <div className="datetime-time-fields">
                <Select
                  aria-label={t('小时', 'Hour')}
                  value={draft.slice(11, 13)}
                  onChange={(event) =>
                    setDraft(`${draft.slice(0, 11)}${event.target.value}${draft.slice(13)}`)
                  }
                >
                  {Array.from({ length: 24 }, (_, i) => (
                    <option key={i} value={pad(i)}>
                      {pad(i)}
                    </option>
                  ))}
                </Select>
                <span aria-hidden="true">:</span>
                <Select
                  aria-label={t('分钟', 'Minute')}
                  value={draft.slice(14, 16)}
                  onChange={(event) => setDraft(`${draft.slice(0, 14)}${event.target.value}`)}
                >
                  {Array.from({ length: 60 }, (_, i) => (
                    <option key={i} value={pad(i)}>
                      {pad(i)}
                    </option>
                  ))}
                </Select>
              </div>
            </div>
            {!validDraft && (
              <p className="datetime-error" role="status">
                {t('请选择有效范围内的日期和时间', 'Choose a valid date and time within the allowed range')}
              </p>
            )}
            <div className="datetime-footer">
              <div className="datetime-shortcuts">
                <button type="button" onClick={() => commit('')}>
                  {t('清除', 'Clear')}
                </button>
                <button type="button" onClick={now}>
                  {t('此刻', 'Now')}
                </button>
              </div>
              <div className="datetime-footer-actions">
                <Popover.Close type="button" className="button secondary">
                  {t('取消', 'Cancel')}
                </Popover.Close>
                <button type="button" className="button" disabled={!validDraft} onClick={() => commit(draft)}>
                  <Check size={14} />
                  {t('确定', 'Apply')}
                </button>
              </div>
            </div>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
    </div>
  );
}
