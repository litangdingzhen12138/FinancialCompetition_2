import type { ChartSpec, DataColumn } from "../types";

function numberValue(value: unknown): number {
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : 0;
}

function displayValue(value: unknown, unit = ""): string {
  if (value === null || value === undefined) return "—";
  if (typeof value === "number") {
    return `${new Intl.NumberFormat("zh-CN", {
      maximumFractionDigits: 4,
    }).format(value)}${unit}`;
  }
  return `${String(value)}${value === "***" ? "" : unit}`;
}

export function SmartChart({
  spec,
  records,
  columns,
  onPointSelect,
}: {
  spec: ChartSpec;
  records: Record<string, unknown>[];
  columns: DataColumn[];
  onPointSelect?: (record: Record<string, unknown>) => void;
}) {
  const xField = spec.x_field;
  const yField = spec.y_fields[0];
  const yColumn = columns.find((column) => column.key === yField);
  const unit = spec.unit || yColumn?.unit || "";

  if (!records.length || spec.chart_type === "table" || !yField) {
    return (
      <div className="empty-chart">
        <span>表</span>
        <strong>当前结果更适合使用表格查看</strong>
        <p>保留完整字段和精确数据，避免不必要的视觉误导。</p>
      </div>
    );
  }

  if (spec.chart_type === "metric") {
    const value = records[0][yField];
    return (
      <div className="metric-visual">
        <p>{spec.title}</p>
        <strong>{displayValue(value, unit)}</strong>
        <span>数据已通过只读查询与结果校验</span>
      </div>
    );
  }

  const points = records.map((record) => ({
    label: String(record[xField || ""] ?? ""),
    value: numberValue(record[yField]),
    record,
  }));
  const values = points.map((point) => point.value);
  const max = Math.max(...values, 1);
  const min = Math.min(...values, 0);
  const span = max - min || 1;

  if (spec.chart_type === "bar") {
    return (
      <div className="bar-chart" role="img" aria-label={spec.title}>
        {points.slice(0, 20).map((point, index) => (
          <div className="bar-row" key={`${point.label}-${index}`}>
            <span className="bar-label" title={point.label}>
              {point.label}
            </span>
            <span className="bar-track">
              <span
                className="bar-fill"
                style={{ width: `${Math.max(4, (point.value / max) * 100)}%` }}
              />
            </span>
            <strong>{displayValue(point.value, unit)}</strong>
          </div>
        ))}
      </div>
    );
  }

  if (spec.chart_type === "donut") {
    const total = values.reduce((sum, value) => sum + Math.max(value, 0), 0) || 1;
    const firstShare = Math.round((Math.max(values[0] || 0, 0) / total) * 100);
    return (
      <div className="donut-wrap">
        <div
          className="donut"
          style={{
            background: `conic-gradient(var(--emerald) 0 ${firstShare}%, var(--gold) ${firstShare}% 100%)`,
          }}
        >
          <span>
            <strong>{displayValue(total, unit)}</strong>
            <small>合计</small>
          </span>
        </div>
        <div className="legend">
          {points.slice(0, 6).map((point, index) => (
            <p key={`${point.label}-${index}`}>
              <span className={index === 0 ? "legend-green" : "legend-gold"} />
              {point.label}
              <strong>{displayValue(point.value, unit)}</strong>
            </p>
          ))}
        </div>
      </div>
    );
  }

  const width = 720;
  const height = 250;
  const padding = 24;
  const coordinates = points.map((point, index) => {
    const x =
      points.length === 1
        ? width / 2
        : padding + (index / (points.length - 1)) * (width - padding * 2);
    const y =
      height - padding - ((point.value - min) / span) * (height - padding * 2);
    return { ...point, x, y };
  });
  const polyline = coordinates.map((point) => `${point.x},${point.y}`).join(" ");

  return (
    <div className="line-chart">
      <svg
        viewBox={`0 0 ${width} ${height}`}
        role="img"
        aria-label={spec.title}
        preserveAspectRatio="none"
      >
        <line x1="24" y1="226" x2="696" y2="226" className="axis-line" />
        <line x1="24" y1="24" x2="24" y2="226" className="axis-line" />
        <polyline points={polyline} className="trend-area-line" />
        {coordinates.map((point, index) => (
          <circle
            key={`${point.label}-${index}`}
            cx={point.x}
            cy={point.y}
            r="5"
            className={
              onPointSelect ? "trend-point interactive" : "trend-point"
            }
            role={onPointSelect ? "button" : undefined}
            tabIndex={onPointSelect ? 0 : undefined}
            aria-label={
              onPointSelect
                ? `选择${point.label}，${displayValue(point.value, unit)}`
                : undefined
            }
            onClick={() => onPointSelect?.(point.record)}
            onKeyDown={(event) => {
              if (onPointSelect && (event.key === "Enter" || event.key === " ")) {
                event.preventDefault();
                onPointSelect(point.record);
              }
            }}
          />
        ))}
      </svg>
      <div className="line-labels">
        {coordinates.slice(0, 8).map((point, index) => (
          <button
            key={`${point.label}-${index}`}
            disabled={!onPointSelect}
            onClick={() => onPointSelect?.(point.record)}
            aria-label={
              onPointSelect
                ? `选择${point.label}继续时间下钻`
                : undefined
            }
          >
            <small>{point.label}</small>
            <strong>{displayValue(point.value, unit)}</strong>
          </button>
        ))}
      </div>
    </div>
  );
}
