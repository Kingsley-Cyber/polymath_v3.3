/**
 * Phase 4.C1 — Books-as-clusters constellation canvas.
 *
 * Renders every document in the selected corpora as a single circle on a
 * Canvas 2D surface. Calls POST /api/graph/by-document with mode="overview"
 * for the gross layout, then mode="drill" when the user clicks a book to
 * expand it (drill state and panel rendering live in BookDrillPanel —
 * this component is the canvas only).
 *
 * Visual mapping:
 *   - Radius          = sqrt(entity_count) * 3
 *   - Fill            = pastel tint of the corpus_id hash
 *   - Stroke          = vivid version of same hash
 *   - Stroke width    = 1.5 (2.5 when hovered, 3.5 when drilled into)
 *   - Hover           = filename tooltip
 *
 * No external graph library (no Sigma, no D3). One Canvas, ~280 lines.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  getGraphByDocument,
  type ByDocumentCluster,
  type ByDocumentEdge,
  type ByDocumentNode,
} from "../../lib/api";
import BookDrillPanel from "./BookDrillPanel";

type Props = {
  corpusIds: string[];
  /** Optional callback when the user opens a book — wire up to chat/discovery. */
  onSelectDoc?: (docId: string) => void;
};

type PlacedDot = ByDocumentCluster & { x: number; y: number; r: number };

// Deterministic HSL by hash so the same corpus always gets the same hue.
const HUE_BY_CORPUS = (corpusId: string): number => {
  let h = 0;
  for (const ch of corpusId) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return h % 360;
};
const STROKE_BY_CORPUS = (corpusId: string, hover = false): string =>
  `hsl(${HUE_BY_CORPUS(corpusId)}, 70%, ${hover ? 38 : 45}%)`;

// Fill is darker than the earlier 92% so dots feel like solid bodies
// against the off-white background. Hovering shifts to 78% for a subtle
// punch; gradient center is 88% so each dot has built-in depth.
const FILL_BY_CORPUS = (corpusId: string, hover = false): string =>
  `hsl(${HUE_BY_CORPUS(corpusId)}, 72%, ${hover ? 78 : 84}%)`;
const FILL_CENTER_BY_CORPUS = (corpusId: string): string =>
  `hsl(${HUE_BY_CORPUS(corpusId)}, 75%, 90%)`;

const layoutDots = (
  clusters: ByDocumentCluster[],
  width: number,
  height: number,
): PlacedDot[] => {
  // Simple circular packing — golden-angle spiral so rings emerge
  // naturally without a hand-tuned ring count.
  const cx = width / 2;
  const cy = height / 2;
  const sorted = [...clusters].sort(
    (a, b) => (b.entity_count || 0) - (a.entity_count || 0),
  );
  const golden = Math.PI * (3 - Math.sqrt(5));
  const placed: PlacedDot[] = [];
  const maxR = Math.min(width, height) * 0.42;
  const total = Math.max(sorted.length, 1);

  for (let i = 0; i < sorted.length; i++) {
    const c = sorted[i];
    const t = (i + 0.5) / total;
    const ringR = Math.sqrt(t) * maxR;
    const angle = i * golden;
    const x = cx + Math.cos(angle) * ringR;
    const y = cy + Math.sin(angle) * ringR;
    const dotR = Math.max(4, Math.sqrt(Math.max(c.entity_count || 1, 1)) * 3);
    placed.push({ ...c, x, y, r: dotR });
  }
  return placed;
};

export default function ConstellationCanvas({
  corpusIds,
  onSelectDoc,
}: Props) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [clusters, setClusters] = useState<ByDocumentCluster[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [hovered, setHovered] = useState<PlacedDot | null>(null);
  const [drilled, setDrilled] = useState<{
    cluster: ByDocumentCluster;
    nodes: ByDocumentNode[];
    edges: ByDocumentEdge[];
  } | null>(null);
  const [drillLoading, setDrillLoading] = useState(false);
  const [size, setSize] = useState({ w: 0, h: 0 });

  // Resize observer — Canvas needs an explicit pixel size, not %.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const ro = new ResizeObserver((entries) => {
      const e = entries[0];
      if (!e) return;
      const rect = e.contentRect;
      setSize({ w: Math.floor(rect.width), h: Math.floor(rect.height) });
    });
    ro.observe(canvas.parentElement || canvas);
    return () => ro.disconnect();
  }, []);

  // Fetch overview when corpus selection changes.
  useEffect(() => {
    if (corpusIds.length === 0) {
      setClusters([]);
      return;
    }
    let cancelled = false;
    setError(null);
    getGraphByDocument({ corpusIds, mode: "overview" })
      .then((res) => {
        if (cancelled) return;
        setClusters(res.clusters || []);
      })
      .catch((exc) => {
        if (cancelled) return;
        setError(exc instanceof Error ? exc.message : String(exc));
      });
    return () => {
      cancelled = true;
    };
  }, [corpusIds.join(",")]);

  const placed = useMemo(
    () => (size.w > 0 ? layoutDots(clusters, size.w, size.h) : []),
    [clusters, size.w, size.h],
  );

  // Top-N book ids (by entity_count) — these get persistent labels so the
  // user always sees the dominant books without having to hover hunt.
  const labeledIds = useMemo(() => {
    const ids = new Set<string>();
    [...placed]
      .sort((a, b) => (b.entity_count || 0) - (a.entity_count || 0))
      .slice(0, Math.min(12, Math.max(4, Math.floor(placed.length * 0.15))))
      .forEach((d) => ids.add(d.cluster_id));
    return ids;
  }, [placed]);

  // Faint connectors between books that share a top-entity name — the
  // "constellation" metaphor needs lines. We compute pairs once per layout
  // change so the draw loop stays fast.
  const constellationLines = useMemo(() => {
    if (placed.length === 0) return [] as Array<[PlacedDot, PlacedDot]>;
    // For each top entity in book A, find the next book B that also lists
    // it. Cap at ~120 lines total so dense corpora don't melt the canvas.
    const byEntity = new Map<string, PlacedDot[]>();
    for (const d of placed) {
      for (const e of (d.top_entity_names ?? []).slice(0, 4)) {
        const k = e.toLowerCase();
        if (!byEntity.has(k)) byEntity.set(k, []);
        byEntity.get(k)!.push(d);
      }
    }
    const lines: Array<[PlacedDot, PlacedDot]> = [];
    for (const dots of byEntity.values()) {
      if (dots.length < 2) continue;
      // Connect each consecutive pair in the bucket (cheap; doesn't fan
      // out into O(n²) cliques on common entities like "Document").
      for (let i = 0; i < dots.length - 1 && lines.length < 120; i++) {
        lines.push([dots[i], dots[i + 1]]);
      }
    }
    return lines;
  }, [placed]);

  // Draw — runs on every relevant state change.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || size.w === 0) return;
    canvas.width = size.w * window.devicePixelRatio;
    canvas.height = size.h * window.devicePixelRatio;
    canvas.style.width = `${size.w}px`;
    canvas.style.height = `${size.h}px`;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.scale(window.devicePixelRatio, window.devicePixelRatio);

    // Background — soft warm off-white.
    ctx.fillStyle = "#FAFAF7";
    ctx.fillRect(0, 0, size.w, size.h);

    // Starfield — deterministic seed from size + variable star sizes so
    // a few bright pinpricks pop against the many small ones. ~110 stars.
    const starSeed = (size.w * 7919 + size.h * 104729) >>> 0;
    let s = starSeed;
    const lcg = () => {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s / 4294967296;
    };
    for (let i = 0; i < 110; i++) {
      const x = lcg() * size.w;
      const y = lcg() * size.h;
      const big = lcg() > 0.93; // ~7% of stars are large
      const r = big ? lcg() * 1.4 + 1.2 : lcg() * 0.55 + 0.25;
      ctx.fillStyle = big ? "rgba(15, 23, 42, 0.42)" : "rgba(15, 23, 42, 0.22)";
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fill();
    }

    // Constellation lines — faint white-grey threads between books that
    // share a top entity. Drawn BEFORE the dots so they sit underneath.
    for (const [a, b] of constellationLines) {
      const dx = a.x - b.x;
      const dy = a.y - b.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const alpha = Math.max(0.04, 0.18 - dist / 1800); // fade with distance
      ctx.strokeStyle = `rgba(60, 80, 105, ${alpha})`;
      ctx.lineWidth = 0.7;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }

    // Book dots — radial gradient fill for depth, drop shadow for lift,
    // smooth hover/drilled state via stroke + shadow boost.
    for (const dot of placed) {
      const isHover = hovered?.cluster_id === dot.cluster_id;
      const isDrilled = drilled?.cluster.cluster_id === dot.cluster_id;

      // Drop shadow (resets after each dot).
      ctx.save();
      ctx.shadowColor =
        isHover || isDrilled
          ? "rgba(15, 23, 42, 0.22)"
          : "rgba(15, 23, 42, 0.08)";
      ctx.shadowBlur = isHover ? 18 : isDrilled ? 14 : 7;
      ctx.shadowOffsetY = isHover ? 4 : 2;

      // Radial gradient — lighter at center, darker at rim.
      const grad = ctx.createRadialGradient(
        dot.x - dot.r * 0.25,
        dot.y - dot.r * 0.3,
        Math.max(1, dot.r * 0.15),
        dot.x,
        dot.y,
        dot.r,
      );
      grad.addColorStop(0, FILL_CENTER_BY_CORPUS(dot.corpus_id));
      grad.addColorStop(1, FILL_BY_CORPUS(dot.corpus_id, isHover));

      ctx.beginPath();
      ctx.arc(dot.x, dot.y, dot.r, 0, Math.PI * 2);
      ctx.fillStyle = grad;
      ctx.fill();
      ctx.restore(); // drop shadow off before stroking

      ctx.strokeStyle = STROKE_BY_CORPUS(dot.corpus_id, isHover);
      ctx.lineWidth = isDrilled ? 3.2 : isHover ? 2.4 : 1.4;
      ctx.beginPath();
      ctx.arc(dot.x, dot.y, dot.r, 0, Math.PI * 2);
      ctx.stroke();
    }

    // Persistent labels for top-N books — small muted text under each.
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    ctx.font = "11px ui-sans-serif, system-ui, -apple-system";
    ctx.fillStyle = "rgba(30, 41, 59, 0.75)";
    for (const dot of placed) {
      if (!labeledIds.has(dot.cluster_id)) continue;
      const label = (dot.label || "").trim();
      if (!label) continue;
      // Trim long filenames so labels don't overlap their neighbors.
      const trimmed = label.length > 22 ? label.slice(0, 21) + "…" : label;
      ctx.fillText(trimmed, dot.x, dot.y + dot.r + 6);
    }

    // Hover label — pill above the hovered dot.
    if (hovered) {
      const label = hovered.label || hovered.cluster_id.slice(0, 16);
      ctx.font =
        "13px ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont";
      ctx.textAlign = "left";
      ctx.textBaseline = "alphabetic";
      const tw = ctx.measureText(label).width + 14;
      const tx = Math.min(Math.max(hovered.x - tw / 2, 6), size.w - tw - 6);
      const ty = Math.max(hovered.y - hovered.r - 26, 6);
      ctx.fillStyle = "rgba(15, 23, 42, 0.92)";
      ctx.beginPath();
      ctx.roundRect(tx, ty, tw, 22, 6);
      ctx.fill();
      ctx.fillStyle = "#FAFAF7";
      ctx.textBaseline = "middle";
      ctx.fillText(label, tx + 7, ty + 11);
    }
  }, [placed, constellationLines, labeledIds, hovered, drilled, size.w, size.h]);

  // Mouse → dot hit testing.
  const hitTest = (
    evt: React.MouseEvent<HTMLCanvasElement>,
  ): PlacedDot | null => {
    const rect = evt.currentTarget.getBoundingClientRect();
    const mx = evt.clientX - rect.left;
    const my = evt.clientY - rect.top;
    for (let i = placed.length - 1; i >= 0; i--) {
      const dot = placed[i];
      const dx = dot.x - mx;
      const dy = dot.y - my;
      if (dx * dx + dy * dy <= dot.r * dot.r) return dot;
    }
    return null;
  };

  const handleClick = async (evt: React.MouseEvent<HTMLCanvasElement>) => {
    const hit = hitTest(evt);
    if (!hit) return;
    setDrillLoading(true);
    try {
      const drill = await getGraphByDocument({
        corpusIds,
        mode: "drill",
        drillDocId: hit.cluster_id,
      });
      setDrilled({
        cluster: hit,
        nodes: drill.nodes || [],
        edges: drill.edges || [],
      });
      onSelectDoc?.(hit.cluster_id);
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : String(exc));
    } finally {
      setDrillLoading(false);
    }
  };

  return (
    <div className="relative w-full h-full">
      <canvas
        ref={canvasRef}
        className="absolute inset-0"
        style={{ cursor: hovered ? "pointer" : "default" }}
        onMouseMove={(e) => setHovered(hitTest(e))}
        onMouseLeave={() => setHovered(null)}
        onClick={handleClick}
      />
      {clusters.length === 0 && !error && (
        <div className="absolute inset-0 flex items-center justify-center text-slate-500">
          {corpusIds.length === 0
            ? "Select a corpus to see the constellation"
            : "Loading…"}
        </div>
      )}
      {error && (
        <div className="absolute top-4 left-4 right-4 bg-rose-50 border border-rose-200 text-rose-800 text-sm rounded p-2">
          {error}
        </div>
      )}
      {drilled && (
        <BookDrillPanel
          cluster={drilled.cluster}
          nodes={drilled.nodes}
          edges={drilled.edges}
          loading={drillLoading}
          onClose={() => setDrilled(null)}
        />
      )}
    </div>
  );
}
