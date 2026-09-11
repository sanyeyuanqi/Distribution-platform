import { ArrowRight } from 'lucide-react';
import { datetime, shortId, Table, useApp } from '../core';
import type { Row } from '../core';
import ChannelUsageTotal from './ChannelUsageTotal';
import { ChannelRemoteStatus, ChannelUploadStatus } from './ChannelDistributionStatus';
import './channel-distribution-table.css';

export default function ChannelDistributionTable({
  rows,
  onDetails,
}: {
  rows: Row[];
  onDetails: (channelId: string) => void;
}) {
  const { t } = useApp();
  return (
    <div className="channel-distribution-table">
      <Table
        rows={rows}
        pageSize={50}
        hidePagination
        scrollLabel={t('站点分发渠道列表', 'Site distribution list')}
        empty={t('暂无符合筛选条件的分发渠道', 'No distributions match these filters')}
        columns={[
          {
            label: t('远端 ID', 'Remote ID'),
            className: 'distribution-id-cell mono',
            render: (r) => r.remote_id || '—',
          },
          {
            label: t('分组标签', 'Group tag'),
            className: 'distribution-tag-cell',
            render: (r) => (
              <span>
                {r.channel.group_tag || r.channel.group_name || shortId(r.channel.group_id)}
                {r.channel.remark && <small className="block muted">{r.channel.remark}</small>}
              </span>
            ),
          },
          {
            label: t('分类', 'Category'),
            className: 'distribution-category-cell',
            render: (r) =>
              t(r.channel.service_name || '—', r.channel.service_name_en || r.channel.service_name || '—'),
          },
          {
            label: t('站点', 'Site'),
            className: 'distribution-site-cell',
            render: (r) => r.site_name || shortId(r.site_id),
          },
          {
            label: t('远端渠道名', 'Remote channel name'),
            className: 'distribution-name-cell',
            render: (r) => r.remote_name || '—',
          },
          {
            label: t('Key 数量', 'Key count'),
            className: 'distribution-count-cell numeric',
            render: (r) => r.key_count ?? 1,
          },
          {
            label: t('上传者', 'Uploader'),
            className: 'distribution-owner-cell',
            render: (r) => r.channel.owner_name || r.channel.owner_username || shortId(r.channel.owner_id),
          },
          {
            label: t('上传状态', 'Upload status'),
            className: 'distribution-upload-cell',
            render: (r) => <ChannelUploadStatus row={r} />,
          },
          {
            label: t('远端状态', 'Remote status'),
            className: 'distribution-status-cell',
            render: (r) => <ChannelRemoteStatus row={r} />,
          },
          {
            label: t('消耗金额', 'Usage amount'),
            className: 'distribution-amount-cell',
            render: (r) => <ChannelUsageTotal channel={r} singleDistribution />,
          },
          {
            label: t('最近同步', 'Last synced'),
            className: 'distribution-date-cell',
            render: (r) => datetime(r.usage_sync?.synced_at || r.last_sync_at),
          },
          {
            label: t('操作', 'Actions'),
            className: 'table-actions-cell distribution-actions-cell',
            render: (r) => (
              <button className="text-button" onClick={() => onDetails(r.channel.id)}>
                {t('查看详情', 'Details')}
                <ArrowRight size={14} />
              </button>
            ),
          },
        ]}
      />
    </div>
  );
}
