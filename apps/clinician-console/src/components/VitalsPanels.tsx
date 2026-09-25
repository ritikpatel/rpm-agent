// Small multiples of vital signs over a shared time axis: one y-axis per panel (never dual-axis), the NEWS2
// zero-point range as a quiet band, a shared crosshair + one tooltip for all series, keyboard navigable.
import { useEffect, useMemo, useRef, useState } from "react";
import type { KeyboardEvent, PointerEvent } from "react";
import type { VitalRow } from "../api";
import { hhmm } from "./ui";

type Key = "heart_rate" | "spo2" | "resp_rate" | "sbp" | "dbp" | "temp" | "news2";
interface Series { key: Key; label: string; color: string }
interface Panel { id: string; title: string; unit: string; series: Series[]; band: [number, number]; digits: number;
                  minSpan: number; step?: boolean }

const S1 = "var(--series-1)", S2 = "var(--series-2)";
const PANELS: Panel[] = [
  { id: "hr", title: "Heart rate", unit: "/min", series: [{ key: "heart_rate", label: "Heart rate", color: S1 }],
    band: [51, 90], digits: 0, minSpan: 20 },
  { id: "spo2", title: "SpO₂", unit: "%", series: [{ key: "spo2", label: "SpO₂", color: S1 }], band: [96, 100],
    digits: 0, minSpan: 6 },
  { id: "rr", title: "Respiratory rate", unit: "/min", series: [{ key: "resp_rate", label: "Resp. rate", color: S1 }],
    band: [12, 20], digits: 0, minSpan: 8 },
  { id: "bp", title: "Blood pressure", unit: "mmHg", band: [111, 219], digits: 0, minSpan: 30,
    series: [{ key: "sbp", label: "Systolic", color: S1 }, { key: "dbp", label: "Diastolic", color: S2 }] },
  { id: "temp", title: "Temperature", unit: "°C", series: [{ key: "temp", label: "Temperature", color: S1 }],
    band: [36.1, 38.0], digits: 1, minSpan: 1.5 },
  { id: "news2", title: "NEWS2", unit: "", series: [{ key: "news2", label: "NEWS2", color: S1 }], band: [0, 4],
    digits: 0, minSpan: 6, step: true },
];

const M = { left: 38, right: 46, top: 6, bottom: 4 };
const PLOT_H = 64;
const AXIS_H = 18;

function niceTicks(lo: number, hi: number, count = 3): number[] {
  const span = hi - lo;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const out: number[] = [];
  for (let v = Math.ceil(lo / step) * step; v <= hi + 1e-9; v += step) out.push(+v.toFixed(6));
  return out;
}

function timeTicks(t0: number, t1: number): number[] {
  const spanMin = (t1 - t0) / 60000;
  const stepMin = spanMin <= 60 ? 10 : spanMin <= 360 ? 60 : 240;
  const step = stepMin * 60000;
  const out: number[] = [];
  for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) out.push(t);
  return out;
}

export default function VitalsPanels({ rows, minutes, now, stale }: { rows: VitalRow[]; minutes: number;
                                                                      now: number; stale?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(640);
  const [hover, setHover] = useState<{ i: number; x: number; y: number } | null>(null);

  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.max(320, e.contentRect.width)));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);

  const t1 = now, t0 = now - minutes * 60000;
  const data = useMemo(() => rows.filter((r) => !r.late).map((r) => ({ ...r, t: new Date(r.ts).getTime() }))
    .filter((r) => r.t >= t0 && r.t <= t1).sort((a, b) => a.t - b.t), [rows, t0, t1]);
  const visible = PANELS.filter((p) => p.series.some((s) => data.some((r) => r[s.key] != null)));
  const missing = PANELS.filter((p) => !visible.includes(p)).map((p) => p.title);
  const plotW = width - M.left - M.right;
  const x = (t: number) => M.left + ((t - t0) / (t1 - t0)) * plotW;
  const gapMs = Math.max(5 * 60000, (minutes * 60000) / 60);

  const onMove = (e: PointerEvent<HTMLDivElement>) => {
    if (!data.length || !ref.current) return;
    const box = ref.current.getBoundingClientRect();
    const px = e.clientX - box.left;
    const t = t0 + ((px - M.left) / plotW) * (t1 - t0);
    let best = 0;
    for (let i = 1; i < data.length; i++) if (Math.abs(data[i].t - t) < Math.abs(data[best].t - t)) best = i;
    setHover({ i: best, x: px, y: e.clientY - box.top });
  };
  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (!data.length) return;
    const cur = hover?.i ?? data.length - 1;
    const next = e.key === "ArrowLeft" ? Math.max(0, cur - 1) : e.key === "ArrowRight"
      ? Math.min(data.length - 1, cur + 1) : e.key === "End" ? data.length - 1 : e.key === "Home" ? 0 : null;
    if (e.key === "Escape") setHover(null);
    if (next == null) return;
    e.preventDefault();
    setHover({ i: next, x: x(data[next].t), y: 40 });
  };

  // latest value at or before the hovered time, per series (sparse home readings still show up)
  const readout = (key: Key, i: number) => {
    for (let j = i; j >= 0; j--) {
      const v = data[j][key];
      if (v != null) return { v, t: data[j].t };
      if (data[i].t - data[j].t > 30 * 60000) break;
    }
    return null;
  };

  if (!data.length) return <div className="empty" ref={ref}>No readings in the last {minutes} minutes.</div>;
  const ticksT = timeTicks(t0, t1);

  return (
    <div className={`panels${stale ? " stale" : ""}`} ref={ref} tabIndex={0} onPointerMove={onMove}
         onPointerLeave={() => setHover(null)} onKeyDown={onKey}
         aria-label={`Vital signs, last ${minutes} minutes. Use left and right arrow keys to read values.`}>
      {visible.map((p, pi) => {
        const vals = data.flatMap((r) => p.series.map((s) => r[s.key]).filter((v): v is number => v != null));
        let lo = Math.min(...vals), hi = Math.max(...vals);
        const pad = Math.max((hi - lo) * 0.12, (p.minSpan - (hi - lo)) / 2, 0.5);
        lo -= pad; hi += pad;
        if (p.id === "spo2") hi = Math.min(hi, 100.5);
        if (p.id === "news2") lo = Math.max(lo, -0.5);
        const y = (v: number) => M.top + (1 - (v - lo) / (hi - lo)) * PLOT_H;
        const last = pi === visible.length - 1;
        const h = M.top + PLOT_H + M.bottom + (last ? AXIS_H : 0);
        const bandLo = Math.max(p.band[0], lo), bandHi = Math.min(p.band[1], hi);
        return (
          <div key={p.id}>
            <div className="panel-title">
              <span>{p.title}{p.unit ? ` (${p.unit})` : ""}</span>
              {p.series.length > 1 && (
                <span className="legend">
                  {p.series.map((s) => (
                    <span className="legend-key" key={s.key}><i style={{ background: s.color }} />{s.label}</span>
                  ))}
                </span>
              )}
            </div>
            <svg width={width} height={h} role="img" aria-label={`${p.title} chart`} style={{ display: "block" }}>
              {bandHi > bandLo && (
                <rect x={M.left} width={plotW} y={y(bandHi)} height={y(bandLo) - y(bandHi)} fill="var(--band)" />
              )}
              <g className="axis">
                {niceTicks(lo, hi).map((v) => (
                  <g key={v}>
                    <line x1={M.left} x2={M.left + plotW} y1={y(v)} y2={y(v)} stroke="var(--grid)" strokeWidth="1" />
                    <text x={M.left - 6} y={y(v) + 3} textAnchor="end">{v}</text>
                  </g>
                ))}
                {last && ticksT.map((t) => (
                  <text key={t} x={x(t)} y={M.top + PLOT_H + M.bottom + 13} textAnchor="middle">{hhmm(t)}</text>
                ))}
              </g>
              {p.series.map((s) => {
                const pts = data.filter((r) => r[s.key] != null);
                let d = "";
                pts.forEach((r, i) => {
                  const gap = i === 0 || r.t - pts[i - 1].t > gapMs;
                  const px = x(r.t).toFixed(1), py = y(r[s.key] as number).toFixed(1);
                  if (gap) d += `M${px},${py}`;
                  else d += p.step ? `H${px}V${py}` : `L${px},${py}`;
                });
                const sparse = pts.length < 40;
                const end = pts[pts.length - 1];
                return (
                  <g key={s.key}>
                    <path d={d} fill="none" stroke={s.color} strokeWidth="2" strokeLinejoin="round"
                          strokeLinecap="round" />
                    {sparse && pts.map((r) => (
                      <circle key={r.t} cx={x(r.t)} cy={y(r[s.key] as number)} r="4" fill={s.color}
                              stroke="var(--surface)" strokeWidth="2" />
                    ))}
                    {end && !sparse && (
                      <circle cx={x(end.t)} cy={y(end[s.key] as number)} r="4" fill={s.color}
                              stroke="var(--surface)" strokeWidth="2" />
                    )}
                    {end && (
                      <text x={M.left + plotW + 8} y={y(end[s.key] as number) + 4} fontSize="11"
                            fill="var(--ink-2)" style={{ fontVariantNumeric: "tabular-nums" }}>
                        {(end[s.key] as number).toFixed(p.digits)}
                      </text>
                    )}
                  </g>
                );
              })}
              {hover && (
                <line x1={x(data[hover.i].t)} x2={x(data[hover.i].t)} y1={M.top} y2={M.top + PLOT_H}
                      stroke="var(--ink-muted)" strokeWidth="1" />
              )}
            </svg>
          </div>
        );
      })}
      {missing.length > 0 && <div className="muted small" style={{ marginTop: 6 }}>Not measured in this window:
        {" "}{missing.join(", ")}. Shaded band = NEWS2 zero-point range.</div>}
      {missing.length === 0 && <div className="muted small" style={{ marginTop: 6 }}>Shaded band = NEWS2
        zero-point range.</div>}
      {hover && (
        <div className="tooltip" role="status"
             style={{ left: Math.min(hover.x + 14, width - 190), top: Math.max(0, hover.y - 20) }}>
          <div className="t">{hhmm(data[hover.i].t)}{data[hover.i].buffered ? " · backfilled" : ""}</div>
          {visible.flatMap((p) => p.series.map((s) => {
            const r = readout(s.key, hover.i);
            return (
              <div className="r" key={s.key}>
                <i style={{ background: s.color }} />
                <b>{r ? (r.v as number).toFixed(p.digits) : "–"}{r && p.unit ? ` ${p.unit}` : ""}</b>
                <span>{s.label}{r && data[hover.i].t - r.t > 120000 ? ` (${hhmm(r.t)})` : ""}</span>
              </div>
            );
          }))}
        </div>
      )}
    </div>
  );
}
