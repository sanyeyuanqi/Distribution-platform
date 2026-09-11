import { useId, useRef, useState } from 'react';
import * as Popover from '@radix-ui/react-popover';
import { ChevronDown, Search, X } from 'lucide-react';
import Tooltip from './Tooltip';
import './routing-group-picker.css';

type Translate = (zh: string, en: string) => string;

export function routingGroups(value: string): string[] {
  return [
    ...new Set(
      value
        .split(',')
        .map((group) => group.trim())
        .filter(Boolean),
    ),
  ];
}

export function routingGroupMaxLength(value: unknown): number {
  return typeof value === 'number' && Number.isInteger(value) && value > 0 ? value : 160;
}

export function routingGroupError(groups: string[], t: Translate, maxLength = 160): string {
  if (!groups.length) return t('请至少选择一个渠道分组。', 'Select at least one channel group.');
  if (groups.length > 32) return t('最多选择 32 个渠道分组。', 'Select at most 32 channel groups.');
  const limit = routingGroupMaxLength(maxLength);
  if (Array.from(groups.join(',')).length > limit)
    return t(
      `此站点渠道分组名称合计不能超过 ${limit} 个字符（含逗号），请减少所选分组。`,
      `This site allows at most ${limit} characters in channel group names, including commas. Select fewer groups.`,
    );
  return '';
}

export default function RoutingGroupPicker({
  value,
  choices,
  verificationKnown = true,
  maxLength = 160,
  disabled,
  onChange,
  t,
}: {
  value: string;
  choices: string[];
  verificationKnown?: boolean;
  maxLength?: number;
  disabled: boolean;
  onChange: (value: string) => void;
  t: Translate;
}) {
  const id = useId();
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const trigger = useRef<HTMLButtonElement>(null);
  const selected = routingGroups(value);
  const limit = routingGroupMaxLength(maxLength);
  const characterCount = Array.from(selected.join(',')).length;
  const characterSummary = t(
    `已选 ${characterCount} / ${limit} 个字符（含逗号）`,
    `${characterCount} / ${limit} characters (including commas)`,
  );
  const available = [...new Set(choices)];
  const unavailable = selected.filter((group) => !available.includes(group));
  const candidates = [...available, ...unavailable];
  const visible = candidates.filter((group) =>
    group.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()),
  );
  const error = routingGroupError(selected, t, limit);
  const changeOpen = (next: boolean) => {
    if (disabled || trigger.current?.matches(':disabled')) return;
    setOpen(next);
    if (!next) setSearch('');
  };
  const toggle = (group: string) => {
    if (disabled) return;
    const next = selected.includes(group)
      ? selected.filter((current) => current !== group)
      : [...selected, group];
    if (!selected.includes(group) && routingGroupError(next, t, limit)) return;
    onChange(next.join(','));
  };
  return (
    <div className="field routing-group-field">
      <span id={`${id}-label`}>{t('目标渠道分组', 'Destination channel groups')}</span>
      <Popover.Root open={open && !disabled} onOpenChange={changeOpen}>
        <Popover.Trigger asChild>
          <button
            ref={trigger}
            type="button"
            className="routing-group-trigger"
            disabled={disabled}
            aria-labelledby={`${id}-label`}
            aria-describedby={`${id}-hint ${id}-status`}
            aria-invalid={!!error}
          >
            <span>
              {selected.length
                ? t(`已选择 ${selected.length} 个分组`, `${selected.length} groups selected`)
                : t('选择渠道分组（可多选）', 'Choose channel groups')}
            </span>
            <ChevronDown size={16} aria-hidden="true" />
          </button>
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content
            className="routing-group-menu"
            align="start"
            sideOffset={6}
            collisionPadding={12}
            aria-label={t('选择目标渠道分组', 'Choose destination channel groups')}
            onInteractOutside={(event) => {
              if (event.target instanceof Element && event.target.closest('.app-tooltip'))
                event.preventDefault();
            }}
            onEscapeKeyDown={(event) => {
              event.preventDefault();
              event.stopPropagation();
              changeOpen(false);
            }}
          >
            <div className="routing-group-search">
              <Search size={15} aria-hidden="true" />
              <input
                type="search"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
                aria-label={t('搜索渠道分组', 'Search channel groups')}
                placeholder={t('搜索渠道分组', 'Search channel groups')}
                autoComplete="off"
              />
            </div>
            <div className="routing-group-options" role="group" aria-labelledby={`${id}-label`}>
              {visible.map((group) => {
                const checked = selected.includes(group);
                const limitReason = checked ? '' : routingGroupError([...selected, group], t, limit);
                const limited = !!limitReason;
                return (
                  <Tooltip key={group} content={limitReason || undefined}>
                    <label className="routing-group-option" data-selected={checked || undefined}>
                      <input
                        type="checkbox"
                        checked={checked}
                        disabled={disabled || limited}
                        onChange={() => toggle(group)}
                        aria-label={group}
                      />
                      <span>{group}</span>
                      {!available.includes(group) && <small>{t('未验证', 'Unverified')}</small>}
                    </label>
                  </Tooltip>
                );
              })}
              {!visible.length && (
                <p className="routing-group-empty">
                  {search
                    ? t('没有匹配的分组。', 'No matching groups.')
                    : t(
                        '暂无已验证分组，请先验证目标站点。',
                        'Verify the destination site to load its groups.',
                      )}
                </p>
              )}
            </div>
            <div className="routing-group-menu-footer">
              <span>
                <span className="block">
                  {t(`已选 ${selected.length} 个 · 最多 32 个`, `${selected.length} selected · Up to 32`)}
                </span>
                <span className="block">{characterSummary}</span>
              </span>
              <Popover.Close type="button" className="text-button">
                {t('完成', 'Done')}
              </Popover.Close>
            </div>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>
      {!!selected.length && (
        <div className="routing-group-tags" aria-label={t('已选渠道分组', 'Selected channel groups')}>
          {selected.map((group) => (
            <span
              className={verificationKnown && unavailable.includes(group) ? 'unverified' : ''}
              key={group}
            >
              <span>{group}</span>
              <button
                type="button"
                disabled={disabled}
                aria-label={t(`移除分组 ${group}`, `Remove group ${group}`)}
                onClick={() => toggle(group)}
              >
                <X size={12} aria-hidden="true" />
              </button>
            </span>
          ))}
        </div>
      )}
      <small id={`${id}-hint`}>
        {t('可多选；创建的渠道会同时加入所选分组。', 'Channels will belong to every selected group.')}
      </small>
      <div id={`${id}-status`} className="routing-group-status" aria-live="polite">
        <small>{characterSummary}</small>
        {error && <small className="danger-text">{error}</small>}
        {verificationKnown && !!unavailable.length && (
          <small className="routing-group-warning">
            {t(
              '部分已选分组未通过当前站点验证，已保留原值；启用前请移除或重新验证站点。',
              'Some saved groups are unverified for this site. Their values are retained; remove them or reverify the site before enabling.',
            )}
          </small>
        )}
      </div>
    </div>
  );
}
