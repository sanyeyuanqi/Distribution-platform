import { useEffect, useId, useRef, useState } from 'react';
import * as Popover from '@radix-ui/react-popover';
import { Check, ChevronDown, LoaderCircle, Search } from 'lucide-react';
import { items, useApp, useData } from '../core';
import { usageAmountText } from '../usageAmounts';
import './channel-category-picker.css';

export type ChannelCategoryOption = {
  category_id: string;
  variant: string;
  name: string;
  name_en: string;
  channel_count: number;
  verified_usage_by_unit: Record<string, string | number | null>;
  data_status: string;
  remote_usage_total?: {
    amount: string | null;
    unit: string;
    covered: number;
    total: number;
  };
};

function optionKey(option: ChannelCategoryOption): string {
  return JSON.stringify([option.category_id, option.variant || '']);
}

export function channelCategoryAmount(option: ChannelCategoryOption): string {
  const total = option.remote_usage_total;
  if (!total) return option.channel_count === 0 ? '$0.00' : '—';
  const text = total.unit === 'USD' ? usageAmountText(total.amount) : null;
  return text === null ? '—' : `$${text}`;
}

export default function ChannelCategoryPicker({
  queryString = '',
  value,
  refreshVersion,
  onChange,
  extraOptions = [],
  categoryIds,
  usageHint,
  required = false,
  placeholder,
  showUsage = true,
  disabled = false,
}: {
  queryString?: string;
  value: ChannelCategoryOption | null;
  refreshVersion: number;
  onChange: (value: ChannelCategoryOption | null) => void;
  extraOptions?: ChannelCategoryOption[];
  categoryIds?: string[];
  usageHint?: string;
  required?: boolean;
  placeholder?: string;
  showUsage?: boolean;
  disabled?: boolean;
}) {
  const { t } = useApp();
  const { data, error, loading, refresh } = useData(
    `/channel-categories${queryString ? `?${queryString}` : ''}`,
  );
  const revision = useRef(refreshVersion);
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [active, setActive] = useState(0);
  const list = useRef<HTMLDivElement>(null);
  const input = useRef<HTMLInputElement>(null);
  const id = useId();
  useEffect(() => {
    if (revision.current !== refreshVersion) {
      revision.current = refreshVersion;
      refresh();
    }
  }, [refreshVersion, refresh]);
  const pending = loading || (!data && !error);
  const options: ChannelCategoryOption[] = [];
  if (!pending && !error) {
    const seen = new Set<string>();
    for (const option of [...(items(data) as ChannelCategoryOption[]), ...extraOptions]) {
      const key = optionKey(option);
      if (seen.has(key) || (categoryIds && !categoryIds.includes(option.category_id))) continue;
      seen.add(key);
      options.push(option);
    }
  }
  const selected = value ? options.find((item) => optionKey(item) === optionKey(value)) : null;
  const selectedName = value
    ? t((selected || value).name, (selected || value).name_en || (selected || value).name)
    : placeholder ||
      (required ? t('选择渠道分类', 'Select a channel category') : t('全部分类', 'All categories'));
  const term = search.trim().toLocaleLowerCase();
  const matches = options.filter((item) =>
    `${item.name} ${item.name_en || ''}`.toLocaleLowerCase().includes(term),
  );
  const choices: (ChannelCategoryOption | null)[] = required ? matches : [null, ...matches];
  const activeIndex = Math.max(0, Math.min(active, choices.length - 1));
  useEffect(() => {
    if (open)
      list.current?.querySelector(`[data-index="${activeIndex}"]`)?.scrollIntoView({ block: 'nearest' });
  }, [activeIndex, open]);
  const changeOpen = (next: boolean) => {
    if (next && disabled) return;
    setOpen(next);
    if (next) {
      setSearch('');
      setActive(0);
    }
  };
  const choose = (option: ChannelCategoryOption | null | undefined) => {
    if (disabled || option === undefined || (required && !option)) return;
    onChange(option);
    setOpen(false);
  };
  return (
    <Popover.Root open={open} onOpenChange={changeOpen}>
      <Popover.Trigger asChild>
        <button
          type="button"
          className="channel-category-trigger"
          role="combobox"
          disabled={disabled}
          aria-required={required || undefined}
          aria-label={t('渠道分类', 'Channel category')}
          aria-haspopup="listbox"
          aria-controls={`${id}-options`}
          onKeyDown={(event) => {
            if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
              event.preventDefault();
              changeOpen(true);
            }
          }}
        >
          <span>
            {selectedName}
            {value && showUsage && (
              <>
                {' '}
                · <strong>{selected ? channelCategoryAmount(selected) : '—'}</strong>
              </>
            )}
          </span>
          {pending ? (
            <LoaderCircle className="spin" size={15} aria-hidden="true" />
          ) : (
            <ChevronDown size={15} aria-hidden="true" />
          )}
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          className="channel-category-menu"
          align="start"
          side="bottom"
          sideOffset={6}
          collisionPadding={12}
          aria-label={t('选择渠道分类', 'Choose channel category')}
          onOpenAutoFocus={(event) => {
            event.preventDefault();
            input.current?.focus();
          }}
          onEscapeKeyDown={(event) => {
            event.preventDefault();
            event.stopPropagation();
            setOpen(false);
          }}
          onKeyDown={(event) => {
            if (!['ArrowDown', 'ArrowUp', 'Home', 'End', 'Enter'].includes(event.key)) return;
            const button = (event.target as HTMLElement).closest('button');
            if (button && button.getAttribute('role') !== 'option') return;
            event.preventDefault();
            if (event.key === 'ArrowDown') setActive((current) => Math.min(current + 1, choices.length - 1));
            if (event.key === 'ArrowUp') setActive((current) => Math.max(current - 1, 0));
            if (event.key === 'Home') setActive(0);
            if (event.key === 'End') setActive(choices.length - 1);
            if (event.key === 'Enter') choose(choices[activeIndex]);
          }}
        >
          <label className="channel-category-search">
            <Search size={15} aria-hidden="true" />
            <input
              ref={input}
              type="search"
              value={search}
              aria-label={t('搜索渠道分类', 'Search channel categories')}
              aria-controls={`${id}-options`}
              aria-activedescendant={choices.length ? `${id}-option-${activeIndex}` : undefined}
              placeholder={t('搜索渠道分类', 'Search channel categories')}
              onChange={(event) => {
                setSearch(event.target.value);
                setActive(!required && event.target.value.trim() ? 1 : 0);
              }}
            />
          </label>
          <div
            className="channel-category-options"
            ref={list}
            id={`${id}-options`}
            role="listbox"
            aria-label={t('渠道分类选项', 'Channel category options')}
          >
            {choices.map((option, index) => {
              const checked = option ? !!value && optionKey(option) === optionKey(value) : !value;
              return (
                <button
                  key={option ? optionKey(option) : 'all'}
                  id={`${id}-option-${index}`}
                  type="button"
                  role="option"
                  tabIndex={-1}
                  aria-selected={checked}
                  data-index={index}
                  data-active={activeIndex === index || undefined}
                  onFocus={() => setActive(index)}
                  onPointerMove={() => setActive(index)}
                  onClick={() => choose(option)}
                >
                  <span>
                    {option ? t(option.name, option.name_en || option.name) : t('全部分类', 'All categories')}
                  </span>
                  {option && showUsage && <strong>{channelCategoryAmount(option)}</strong>}
                  <Check size={14} aria-hidden="true" className={checked ? '' : 'hidden-check'} />
                </button>
              );
            })}
            {pending && (
              <p role="status">
                {showUsage
                  ? t('正在读取分类及消耗…', 'Loading categories and usage…')
                  : t('正在读取分类…', 'Loading categories…')}
              </p>
            )}
            {error && (
              <div className="channel-category-error" role="alert">
                <p>
                  {showUsage
                    ? t('分类及消耗读取失败，请重试。', 'Could not load categories and usage. Retry.')
                    : t('分类读取失败，请重试。', 'Could not load categories. Retry.')}
                </p>
                <button type="button" className="text-button" onClick={refresh}>
                  {t('重试', 'Retry')}
                </button>
              </div>
            )}
            {!pending && !error && !matches.length && (
              <p>
                {term
                  ? t('没有匹配的分类。', 'No matching categories.')
                  : t('暂无可选分类。', 'No categories available.')}
              </p>
            )}
          </div>
          {showUsage && (
            <small className="channel-category-hint">
              {usageHint || t('各站点消耗金额合计（USD）', 'Total site usage amount (USD)')}
            </small>
          )}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
