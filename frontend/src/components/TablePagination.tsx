import { ChevronLeft, ChevronRight } from 'lucide-react';
import { useApp } from '../core';

export default function TablePagination({
  offset,
  limit,
  total,
  loading,
  onChange,
}: {
  offset: number;
  limit: number;
  total: number;
  loading: boolean;
  onChange: (offset: number) => void;
}) {
  const { t } = useApp();
  const pages = Math.max(1, Math.ceil(total / limit));
  return (
    <div className="table-footer" aria-busy={loading}>
      <span>{loading ? t('加载中…', 'Loading…') : t(`共 ${total} 条记录`, `${total} records`)}</span>
      <div className="actions">
        <button
          className="icon-button"
          aria-label={t('上一页', 'Previous page')}
          disabled={loading || offset === 0}
          onClick={() => onChange(Math.max(0, offset - limit))}
        >
          <ChevronLeft size={16} />
        </button>
        <span>{loading ? '—' : `${Math.floor(offset / limit) + 1} / ${pages}`}</span>
        <button
          className="icon-button"
          aria-label={t('下一页', 'Next page')}
          disabled={loading || offset + limit >= total}
          onClick={() => onChange(offset + limit)}
        >
          <ChevronRight size={16} />
        </button>
      </div>
    </div>
  );
}
