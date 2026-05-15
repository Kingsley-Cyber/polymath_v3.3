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

const STROKE_BY_CORPUS = (corpusId: string): string => {
  // Deterministic HSL by hash so the same corpus always gets the same hue.
  let h = 0;
  for (const ch of corpusId) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `hsl(${h % 360}, 65%, 45%)`;
};

const FILL_BY_CORPUS = (corpusId: string): string => {
  let h = 0;
  for (const ch of corpusId) h = (h * 31 + ch.charCodeAt(0)) >>> 0;
  return `hsl(${h % 360}, 70%, 92%)`;
};

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

    // Background — soft starfield (deterministic seed from size).
    ctx.fillStyle = "#FAFAF7";
    ctx.fillRect(0, 0, size.w, size.h);
    const starSeed = (size.w * 7919 + size.h * 104729) >>> 0;
    let s = starSeed;
    const lcg = () => {
      s = (s * 1664525 + 1013904223) >>> 0;
      return s / 4294967296;
    };
    ctx.fillStyle = "rgba(15, 23, 42, 0.18)";
    for (let i = 0; i < 240; i++) {
      const x = lcg() * size.w;
      const y = lcg() * size.h;
      const r = lcg() * 1.1 + 0.2;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fill();
    }

    // Book dots.
    for (const dot of placed) {
      const isHover = hovered?.cluster_id === dot.cluster_id;
      const isDrilled = drilled?.cluster.cluster_id === dot.cluster_id;
      ctx.beginPath();
      ctx.arc(dot.x, dot.y, dot.r, 0, Math.PI * 2);
      ctx.fillStyle = FILL_BY_CORPUS(dot.corpus_id);
      ctx.fill();
      ctx.strokeStyle = STROKE_BY_CORPUS(dot.corpus_id);
      ctx.lineWidth = isDrilled ? 3.5 : isHover ? 2.5 : 1.5;
      ctx.stroke();
    }

    // Hover label.
    if (hovered) {
      const label = hovered.label || hovered.cluster_id.slice(0, 16);
      ctx.font =
        "13px ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont";
      const tw = ctx.measureText(label).width + 12;
      const tx = Math.min(Math.max(hovered.x - tw / 2, 6), size.w - tw - 6);
      const ty = Math.max(hovered.y - hovered.r - 22, 6);
      ctx.fillStyle = "rgba(15, 23, 42, 0.92)";
      ctx.beginPath();
      ctx.roundRect(tx, ty, tw, 22, 6);
      ctx.fill();
      ctx.fillStyle = "#FAFAF7";
      ctx.textBaseline = "middle";
      ctx.fillText(label, tx + 6, ty + 11);
    }
  }, [placed, hovered, drilled, size.w, size.h]);

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
        className="absolute inset-0 cursor-pointer"
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
