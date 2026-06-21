/**
 * Phase 4.C1 — Books-as-clusters constellation canvas.
 *
 * Renders every document in the selected corpora as a single circle on a
 * Canvas 2D surface. Calls POST /api/graph/by-document with mode="overview"
 * for the gross layout, then mode="drill" when the user clicks a book to
 * expand it (drill state and panel rendering live in BookDrillPanel —
 * this component is the canvas only).
 *
 * Premium redesign (Pt 7d):
 *   - Backdrop: research substrate (warm off-white → very faint grid).
 *   - Dots: deterministic HSL hash by corpus_id with brightness ramp by
 *     entity_count (well-connected books read brighter, isolated ones
 *     dimmer) so the same-corpus cluster reads as one hue family.
 *   - Labels: bounded top-N so a 1k-book corpora doesn't blanket the
 *     canvas. Hovered + drilled labels use a pill.
 *   - Subtle ring grid (60px) — research instrument feel, not decoration.
 *   - Hovered dot lifts with a soft shadow; drilled dot gets a ring.
 *   - Click → drill panel (BookDrillPanel).
 */

import { useEffect, useMemo, useRef, useState } from "react";
import {
  getGraphByDocument,
  type ByDocumentCluster,
  type ByDocumentEdge,
  type ByDocumentNode,
} from "../../lib/api";
import BookDrillPanel from "./BookDrillPanel";
import { graphColors } from "../../lib/graph-colors";

type Props = {
  corpusIds: string[];
  /** Optional callback when the user opens a book — wire up to chat/discovery. */
  onSelectDoc?: (docId: string) => void;
};

type PlacedDot = ByDocumentCluster & { x: number; y: number; r: number };

// Deterministic HSL by hash so the same corpus always gets the same hue.
// Brightness ramp by entity_count: low-entity books stay slightly muted,
// well-connected books read brighter. Caps at 5 buckets so the hue
// family stays coherent.
function hashKey(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return h >>> 0;
}

function bucketLightness(entityCount: number): number {
  const buckets = [62, 68, 73, 78, 82];
  return buckets[Math.min(buckets.length - 1, Math.floor(Math.log2(Math.max(entityCount, 1)) / 2))];
}

const HUE_BY_CORPUS = (corpusId: string): number => hashKey(corpusId) % 360;

const STROKE_BY_CORPUS = (corpusId: string, hover = false): string =>
  `hsl(${HUE_BY_CORPUS(corpusId)}, 70%, ${hover ? 30 : 38}%)`;

const FILL_BY_CORPUS = (corpusId: string, entityCount: number): string =>
  `hsl(${HUE_BY_CORPUS(corpusId)}, 68%, ${bucketLightness(entityCount)}%)`;

const FILL_CENTER_BY_CORPUS = (corpusId: string, entityCount: number): string =>
  `hsl(${HUE_BY_CORPUS(corpusId)}, 75%, ${bucketLightness(entityCount) + 7}%)`;

const layoutDots = (
  clusters: ByDocumentCluster[],
  width: number,
  height: number,
): PlacedDot[] => {
  // Golden-angle spiral — natural rings without a hand-tuned ring count.
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
    const dotR = Math.max(5, Math.sqrt(Math.max(c.entity_count || 1, 1)) * 3.2);
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
  // "constellation" metaphor needs lines. Cap at ~120 lines total so
  // dense corpora don't melt the canvas.
  const constellationLines = useMemo(() => {
    if (placed.length === 0) return [] as Array<[PlacedDot, PlacedDot]>;
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
      for (let i = 0; i < dots.length - 1 && lines.length < 120; i++) {
        lines.push([dots[i], dots[i + 1]]);
      }
    }
    return lines;
  }, [placed]);

  // Draw.
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

    // Background — research substrate.
    ctx.fillStyle = graphColors.atomic.background;
    ctx.fillRect(0, 0, size.w, size.h);

    // Faint research grid (60px) — instrument feel, not decoration.
    ctx.strokeStyle = "rgba(15, 23, 42, 0.045)";
    ctx.lineWidth = 1;
    const gridStep = 60;
    ctx.beginPath();
    for (let x = 0; x < size.w; x += gridStep) {
      ctx.moveTo(x, 0);
      ctx.lineTo(x, size.h);
    }
    for (let y = 0; y < size.h; y += gridStep) {
      ctx.moveTo(0, y);
      ctx.lineTo(size.w, y);
    }
    ctx.stroke();

    // Constellation lines.
    for (const [a, b] of constellationLines) {
      const dx = a.x - b.x;
      const dy = a.y - b.y;
      const dist = Math.sqrt(dx * dx + dy * dy);
      const alpha = Math.max(0.05, 0.22 - dist / 1800);
      ctx.strokeStyle = `rgba(60, 80, 105, ${alpha})`;
      ctx.lineWidth = 0.7;
      ctx.beginPath();
      ctx.moveTo(a.x, a.y);
      ctx.lineTo(b.x, b.y);
      ctx.stroke();
    }

    // Book dots — radial gradient fill, drop shadow for lift.
    for (const dot of placed) {
      const isHover = hovered?.cluster_id === dot.cluster_id;
      const isDrilled = drilled?.cluster.cluster_id === dot.cluster_id;

      ctx.save();
      ctx.shadowColor =
        isHover || isDrilled
          ? "rgba(15, 23, 42, 0.28)"
          : "rgba(15, 23, 42, 0.10)";
      ctx.shadowBlur = isHover ? 22 : isDrilled ? 16 : 8;
      ctx.shadowOffsetY = isHover ? 5 : 2;

      const grad = ctx.createRadialGradient(
        dot.x - dot.r * 0.25,
        dot.y - dot.r * 0.3,
        Math.max(1, dot.r * 0.15),
        dot.x,
        dot.y,
        dot.r,
      );
      const eCount = dot.entity_count || 1;
      grad.addColorStop(0, FILL_CENTER_BY_CORPUS(dot.corpus_id, eCount));
      grad.addColorStop(1, FILL_BY_CORPUS(dot.corpus_id, eCount));

      ctx.beginPath();
      ctx.arc(dot.x, dot.y, dot.r, 0, Math.PI * 2);
      ctx.fillStyle = grad;
      ctx.fill();
      ctx.restore();

      ctx.strokeStyle = STROKE_BY_CORPUS(dot.corpus_id, isHover);
      ctx.lineWidth = isDrilled ? 3.2 : isHover ? 2.4 : 1.4;
      ctx.beginPath();
      ctx.arc(dot.x, dot.y, dot.r, 0, Math.PI * 2);
      ctx.stroke();

      // Drilled dot gets a ring offset to read as "active".
      if (isDrilled) {
        ctx.strokeStyle = "rgba(15, 23, 42, 0.6)";
        ctx.lineWidth = 1;
        ctx.beginPath();
        ctx.arc(dot.x, dot.y, dot.r + 6, 0, Math.PI * 2);
        ctx.stroke();
      }
    }

    // Persistent labels for top-N books.
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (const dot of placed) {
      if (!labeledIds.has(dot.cluster_id)) continue;
      const label = (dot.label || "").trim();
      if (!label) continue;
      const trimmed = label.length > 24 ? label.slice(0, 23) + "…" : label;
      const padX = 6;
      ctx.font =
        "10.5px ui-sans-serif, system-ui, -apple-system, Inter, BlinkMacSystemFont";
      const tw = ctx.measureText(trimmed).width;
      const w = tw + padX * 2;
      const h = 16;
      const x = dot.x - w / 2;
      const y = dot.y + dot.r + 6;
      // Pill background — slightly tinted slate.
      ctx.fillStyle = "rgba(15, 23, 42, 0.85)";
      ctx.beginPath();
      if (typeof ctx.roundRect === "function") {
        ctx.roundRect(x, y, w, h, 4);
      } else {
        ctx.rect(x, y, w, h);
      }
      ctx.fill();
      ctx.fillStyle = "#e2e8f0";
      ctx.textBaseline = "middle";
      ctx.fillText(trimmed, dot.x, y + h / 2);
    }

    // Hover pill (above hovered dot).
    if (hovered) {
      const label = hovered.label || hovered.cluster_id.slice(0, 16);
      ctx.font =
        "13px ui-sans-serif, system-ui, -apple-system, Inter, BlinkMacSystemFont";
      ctx.textAlign = "left";
      ctx.textBaseline = "alphabetic";
      const tw = ctx.measureText(label).width + 16;
      const tx = Math.min(Math.max(hovered.x - tw / 2, 8), size.w - tw - 8);
      const ty = Math.max(hovered.y - hovered.r - 28, 8);
      ctx.fillStyle = graphColors.atomic.nucleus;
      ctx.beginPath();
      if (typeof ctx.roundRect === "function") {
        ctx.roundRect(tx, ty, tw, 24, 6);
      } else {
        ctx.rect(tx, ty, tw, 24);
      }
      ctx.fill();
      ctx.fillStyle = graphColors.atomic.background;
      ctx.textBaseline = "middle";
      ctx.fillText(label, tx + 8, ty + 12);
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
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1.5 text-content-tertiary">
          {corpusIds.length === 0 ? (
            <>
              <div className="text-sm font-medium text-content-secondary">
                Select a corpus
              </div>
              <div className="text-[11px] font-mono">
                Books will appear as connected points
              </div>
            </>
          ) : (
            <div className="flex items-center gap-2 text-[11px] font-mono">
              <div className="h-1.5 w-1.5 animate-pulse rounded-full bg-accent-main" />
              Building constellation…
            </div>
          )}
        </div>
      )}
      {error && (
        <div className="absolute top-4 left-4 right-4 rounded-md border border-error/40 bg-[var(--bg-raised)] p-3 text-sm text-error">
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