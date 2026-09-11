import { useState } from 'react';
import { BarChart3 } from 'lucide-react';
import { Empty, money, useApp } from '../core';
import type { Row } from '../core';
import Tooltip from '../components/Tooltip';

export default function UsageTrend({ trend, partial }: { trend: Row[]; partial: boolean }) {
  const { t } = useApp();
  const units = Array.from(new Set(trend.flatMap((d) => Object.keys(d.totals_by_unit || {}))));
  const [chosen, setChosen] = useState('');
  const unit = units.includes(chosen) ? chosen : units[0];
  const [hover, setHover] = useState<number | null>(null);
  const values = trend.map((d) => {
    const value = d.totals_by_unit?.[unit];
    return value === undefined || value === null ? null : Number(value);
  });
  const maximum = Math.max(...values.filter((value): value is number => value !== null), 1);
  const width = 760,
    height = 175,
    left = 58,
    right = 16,
    top = 12,
    bottom = 28;
  const x = (i: number) => left + ((width - left - right) * i) / Math.max(trend.length - 1, 1);
  const y = (n: number) => top + (height - top - bottom) * (1 - n / maximum);
  const pointLabel = (i: number) =>
    `${trend[i].date} · ${values[i] === null ? t('无已核实数据', 'No verified data') : `${money(values[i], 4)} ${unit}`}`;
  const segments: number[][] = [];
  let segment: number[] = [];
  values.forEach((value, index) => {
    if (value === null) {
      segment = [];
      return;
    }
    if (!segment.length) segments.push(segment);
    segment.push(index);
  });
  return (
    <section className="panel usage-trend">
      <div className="panel-heading">
        <div>
          <h2>{t('近 14 天消耗趋势', 'Usage over the last 14 days')}</h2>
          <p>
            {t(
              '已核实来源 · Asia/Shanghai · 缺口单独标记',
              'Verified sources · Asia/Shanghai · Gaps reported separately',
            )}
            {partial ? ` · ${t('部分覆盖', 'Partial coverage')}` : ''}
          </p>
        </div>
        {units.length > 0 ? (
          <div className="segmented small">
            {units.map((u) => (
              <button key={u} className={u === unit ? 'selected' : ''} onClick={() => setChosen(u)}>
                {u}
              </button>
            ))}
          </div>
        ) : (
          <BarChart3 size={18} />
        )}
      </div>
      {units.length ? (
        <div className="trend-chart">
          <div className="trend-hover">
            {hover !== null
              ? pointLabel(hover)
              : t('悬停或聚焦查看每日已核实消耗', 'Hover or focus to inspect daily verified usage')}
          </div>
          <svg
            viewBox={`0 0 ${width} ${height}`}
            role="img"
            aria-label={t(`最近 14 天 ${unit} 消耗趋势`, `Verified ${unit} usage over 14 days`)}
          >
            {[0, 0.5, 1].map((tick) => (
              <g key={tick}>
                <line
                  x1={left}
                  x2={width - right}
                  y1={y(maximum * tick)}
                  y2={y(maximum * tick)}
                  stroke="#edf1f7"
                  strokeDasharray="4 4"
                />
                <text x={left - 11} y={y(maximum * tick) + 3} textAnchor="end" fill="#a2aec1" fontSize="9">
                  {money(maximum * tick, maximum > 1000 ? 0 : 2)}
                </text>
              </g>
            ))}
            {segments.map((indices) => (
              <polyline
                key={indices[0]}
                points={indices.map((index) => `${x(index)},${y(values[index]!)}`).join(' ')}
                fill="none"
                stroke="#a396df"
                strokeWidth="2.3"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            ))}
            {values.map((v, i) => (
              <g key={i}>
                <Tooltip content={pointLabel(i)}>
                  <rect
                    x={x(i) - 18}
                    y={top}
                    width={36}
                    height={height - top - bottom}
                    fill="transparent"
                    onMouseEnter={() => setHover(i)}
                    onMouseLeave={() => setHover(null)}
                    onFocus={() => setHover(i)}
                    onBlur={() => setHover(null)}
                    tabIndex={0}
                    aria-label={pointLabel(i)}
                  />
                </Tooltip>
                {v !== null && (
                  <circle
                    cx={x(i)}
                    cy={y(v)}
                    r={hover === i ? 4 : 3}
                    fill="#8f7bce"
                    stroke="white"
                    strokeWidth="2"
                    pointerEvents="none"
                  />
                )}
                {(i === 0 || i === values.length - 1 || i % 3 === 0) && (
                  <text x={x(i)} y={height - 7} textAnchor="middle" fill="#a2aec1" fontSize="9">
                    {trend[i].date?.slice(5)}
                  </text>
                )}
              </g>
            ))}
          </svg>
        </div>
      ) : (
        <Empty text={t('尚无可绘制的已核实消耗', 'No verified usage to chart yet')} />
      )}
    </section>
  );
}
