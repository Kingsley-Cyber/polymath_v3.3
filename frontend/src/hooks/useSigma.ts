/**
 * Polymath sigma.js integration hook — port of GitNexus's
 * `gitnexus-web/src/hooks/useSigma.ts` (reference:
 * github.com/abhigyanpatwari/GitNexus). Same physics, same nodeReducer /
 * edgeReducer pattern, same hover pill, same FA2 worker layout with
 * adaptive settings and noverlap cleanup pass.
 *
 * Polymath-specific additions:
 *   • Node drag (mouse down → mousemovebody captor → write graph coords)
 *   • Optional onDoubleClickNode callback for drill-into-cluster / drill-
 *     into-book navigation
 *   • SigmaNodeAttributes / SigmaEdgeAttributes from the polymath-graph-
 *     adapter (instead of GitNexus's KnowledgeGraph types)
 */

import { useRef, useEffect, useCallback, useState } from "react";
import Sigma from "sigma";
import Graph from "graphology";
import FA2Layout from "graphology-layout-forceatlas2/worker";
import forceAtlas2 from "graphology-layout-forceatlas2";
import noverlap from "graphology-layout-noverlap";
import EdgeCurveProgram from "@sigma/edge-curve";
import BookGlowProgram from "../lib/sigma-programs/BookGlowProgram";
import type {
  SigmaNodeAttributes,
  SigmaEdgeAttributes,
} from "../lib/polymath-graph-adapter";
import type { QueryFingerprint } from "../lib/query-fingerprint";
import { motion as motionTokens } from "../lib/design-tokens";

// ── Color helpers (verbatim from GitNexus useSigma.ts) ────────────────────

const hexToRgb = (hex: string): { r: number; g: number; b: number } => {
  const result = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  return result
    ? {
        r: parseInt(result[1], 16),
        g: parseInt(result[2], 16),
        b: parseInt(result[3], 16),
      }
    : { r: 100, g: 100, b: 100 };
};

const rgbToHex = (r: number, g: number, b: number): string => {
  return (
    "#" +
    [r, g, b]
      .map((x) => {
        const hex = Math.max(0, Math.min(255, Math.round(x))).toString(16);
        return hex.length === 1 ? "0" + hex : hex;
      })
      .join("")
  );
};

// Mix toward dark background — keeps a hint of color when dimmed.
// Uses the new research-substrate tone (#0c0e13) so unselected nodes
// dim toward the actual canvas background instead of the old night-sky
// tone, keeping the canvas cohesive.
const dimColor = (hex: string, amount: number): string => {
  const rgb = hexToRgb(hex);
  const darkBg = { r: 12, g: 14, b: 19 };
  return rgbToHex(
    darkBg.r + (rgb.r - darkBg.r) * amount,
    darkBg.g + (rgb.g - darkBg.g) * amount,
    darkBg.b + (rgb.b - darkBg.b) * amount,
  );
};

const brightenColor = (hex: string, factor: number): string => {
  const rgb = hexToRgb(hex);
  return rgbToHex(
    rgb.r + ((255 - rgb.r) * (factor - 1)) / factor,
    rgb.g + ((255 - rgb.g) * (factor - 1)) / factor,
    rgb.b + ((255 - rgb.b) * (factor - 1)) / factor,
  );
};

// ── Hook contract ────────────────────────────────────────────────────────

interface UseSigmaOptions {
  onNodeClick?: (nodeId: string) => void;
  onNodeHover?: (nodeId: string | null) => void;
  onStageClick?: () => void;
  onDoubleClickNode?: (nodeId: string) => void;
  highlightedNodeIds?: Set<string>;
  layoutMode?: "brain" | "query";
  queryFingerprint?: QueryFingerprint;
  /** Pt 6: when true, FA2 restarts for ~5s after a node-drag release so
   *  the neighbors re-arrange around the new position. Default false. */
  settleAfterDrag?: boolean;
}

interface UseSigmaReturn {
  containerRef: React.RefObject<HTMLDivElement | null>;
  sigmaRef: React.RefObject<Sigma | null>;
  setGraph: (graph: Graph<SigmaNodeAttributes, SigmaEdgeAttributes>) => void;
  zoomIn: () => void;
  zoomOut: () => void;
  resetZoom: () => void;
  focusNode: (nodeId: string) => void;
  isLayoutRunning: boolean;
  startLayout: () => void;
  stopLayout: () => void;
  selectedNode: string | null;
  setSelectedNode: (nodeId: string | null) => void;
  refreshHighlights: () => void;
}

// ── ForceAtlas2 + noverlap settings ─────────────────────────────────────

// Anti-overlap pass is now more aggressive — ratio 1.35 + margin 22 + 80
// iterations carves real breathing room between nodes after FA2 settles.
// Without this the no-overlap step had too little authority to undo
// FA2's central clumping.
const getNoverlapSettings = (
  nodeCount: number,
  mode: "brain" | "query",
) => {
  if (mode !== "query") {
    return {
      maxIterations: nodeCount > 2500 ? 70 : 90,
      ratio: nodeCount > 2500 ? 1.08 : 1.14,
      margin: nodeCount > 2500 ? 4 : 6,
      expansion: 1.04,
    };
  }
  return {
    maxIterations: nodeCount > 500 ? 170 : 150,
    ratio: nodeCount > 500 ? 1.85 : 2.05,
    margin: nodeCount > 500 ? 42 : 54,
    expansion: 1.42,
  };
};

const getFA2Settings = (
  nodeCount: number,
  mode: "brain" | "query",
  fingerprint?: QueryFingerprint,
) => {
  const queryPhysics = {
    repulsion: 5.25 * (fingerprint?.repulsionMultiplier ?? 1),
    spring: 1.15 * (fingerprint?.springMultiplier ?? 1),
    damping: 1.8 * (fingerprint?.dampingMultiplier ?? 1),
  };
  const base = {
    gravity: 0.6,
    scalingRatio: 22,
    slowDown: 3,
    barnesHutOptimize: nodeCount > 200,
    barnesHutTheta: 0.6,
    strongGravityMode: false,
    outboundAttractionDistribution: true,
    linLogMode: false,
    adjustSizes: true,
    edgeWeightInfluence: 1.0,
  };

  if (mode !== "query") {
    return {
      ...base,
      gravity: nodeCount > 1000 ? 0.008 : 0.014,
      scalingRatio: nodeCount > 1000 ? 118 : 94,
      slowDown: nodeCount > 1000 ? 7.2 : 6.0,
      barnesHutOptimize: nodeCount > 120,
      barnesHutTheta: 0.78,
      linLogMode: true,
      edgeWeightInfluence: 0.12,
    };
  }

  // Query graphs should read as a curated atom: more breathing room,
  // weaker center gravity, and slightly stronger edge weights so seeds,
  // hubs, and direct evidence connections stay visually coherent.
  if (nodeCount > 800) {
    return {
      ...base,
      gravity: 0.055,
      scalingRatio: 52 * queryPhysics.repulsion,
      slowDown: 5.8 * queryPhysics.damping,
      barnesHutOptimize: true,
      barnesHutTheta: 0.7,
      edgeWeightInfluence: 0.8 * queryPhysics.spring,
    };
  }
  if (nodeCount > 200) {
    return {
      ...base,
      gravity: 0.07,
      scalingRatio: 46 * queryPhysics.repulsion,
      slowDown: 5.2 * queryPhysics.damping,
      barnesHutOptimize: true,
      edgeWeightInfluence: 0.82 * queryPhysics.spring,
    };
  }
  return {
    ...base,
    gravity: 0.09,
    scalingRatio: 40 * queryPhysics.repulsion,
    slowDown: 4.8 * queryPhysics.damping,
    barnesHutOptimize: nodeCount > 120,
    edgeWeightInfluence: 0.85 * queryPhysics.spring,
  };
};

function enforceQueryMinimumSpacing(
  graph: Graph<SigmaNodeAttributes, SigmaEdgeAttributes>,
  passes = 8,
) {
  const nodes = graph.nodes();
  const nodeCount = nodes.length;
  if (nodeCount < 2) return;

  const scale = Math.sqrt(Math.max(nodeCount, 80));
  const baseGap = nodeCount > 450 ? 7.4 : nodeCount > 180 ? 9.0 : 11.2;
  const minFor = (id: string) => {
    const size = Number(graph.getNodeAttribute(id, "size") ?? 8);
    const forced = Boolean(graph.getNodeAttribute(id, "forceLabel"));
    return Math.max(scale * baseGap, size * (forced ? 9 : 7));
  };

  for (let pass = 0; pass < passes; pass += 1) {
    let moved = false;
    for (let i = 0; i < nodeCount; i += 1) {
      const a = nodes[i];
      let ax = Number(graph.getNodeAttribute(a, "x") ?? 0);
      let ay = Number(graph.getNodeAttribute(a, "y") ?? 0);
      for (let j = i + 1; j < nodeCount; j += 1) {
        const b = nodes[j];
        let bx = Number(graph.getNodeAttribute(b, "x") ?? 0);
        let by = Number(graph.getNodeAttribute(b, "y") ?? 0);
        let dx = ax - bx;
        let dy = ay - by;
        let dist = Math.sqrt(dx * dx + dy * dy);
        if (!Number.isFinite(dist) || dist < 0.001) {
          const seed = `${a}:${b}`;
          let h = 2166136261;
          for (let k = 0; k < seed.length; k += 1) {
            h ^= seed.charCodeAt(k);
            h = Math.imul(h, 16777619);
          }
          const angle = ((h >>> 0) / 4294967295) * Math.PI * 2;
          dx = Math.cos(angle) * 0.001;
          dy = Math.sin(angle) * 0.001;
          dist = 0.001;
        }
        const minDist = (minFor(a) + minFor(b)) * 0.5;
        if (dist >= minDist) continue;

        const push = ((minDist - dist) / dist) * 0.62;
        const px = dx * push;
        const py = dy * push;
        ax += px;
        ay += py;
        bx -= px;
        by -= py;
        graph.setNodeAttribute(b, "x", bx);
        graph.setNodeAttribute(b, "y", by);
        moved = true;
      }
      graph.setNodeAttribute(a, "x", ax);
      graph.setNodeAttribute(a, "y", ay);
    }
    if (!moved) break;
  }
}

function stableUnitFromString(value: string): number {
  let h = 2166136261;
  for (let i = 0; i < value.length; i += 1) {
    h ^= value.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0) / 4294967295;
}

function finiteNumber(value: unknown, fallback = 0): number {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function brainClusterTarget(
  key: string,
  index: number,
  total: number,
  spread: number,
): { x: number; y: number } {
  if (total <= 1) return { x: 0, y: 0 };
  const goldenAngle = Math.PI * (3 - Math.sqrt(5));
  if (total > 90) {
    const radius =
      spread *
      (0.08 + 0.94 * Math.sqrt((index + 1) / Math.max(total, 1)));
    const angle =
      index * goldenAngle + stableUnitFromString(`${key}:brain-center`) * 0.22;
    return { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
  }
  const ring = Math.max(1, Math.ceil(Math.sqrt(total)));
  const radius =
    spread *
    (0.52 + 0.2 * Math.floor(index / ring)) *
    Math.sqrt((index + 1) / total);
  const angle = index * goldenAngle + stableUnitFromString(`${key}:brain-center`) * 0.45;
  return { x: Math.cos(angle) * radius, y: Math.sin(angle) * radius };
}

function enforceBrainClusterSpacing(
  graph: Graph<SigmaNodeAttributes, SigmaEdgeAttributes>,
  passes = 4,
) {
  const nodes = graph.nodes();
  const nodeCount = nodes.length;
  if (nodeCount < 2) return;

  const groups = new Map<string, string[]>();
  for (const id of nodes) {
    const key = String(
      graph.getNodeAttribute(id, "brain_cluster_key") ||
        graph.getNodeAttribute(id, "source_corpus") ||
        graph.getNodeAttribute(id, "nodeKind") ||
        "corpus",
    );
    const list = groups.get(key) || [];
    list.push(id);
    groups.set(key, list);
  }

  const orderedGroups = [...groups.entries()].sort(
    (a, b) => b[1].length - a[1].length || a[0].localeCompare(b[0]),
  );
  const spread = Math.sqrt(Math.max(nodeCount, 120)) * (nodeCount > 1500 ? 30 : 36);
  orderedGroups.forEach(([key, groupNodes], idx) => {
    if (groupNodes.length === 0) return;
    const target = brainClusterTarget(key, idx, orderedGroups.length, spread);
    let cx = 0;
    let cy = 0;
    for (const id of groupNodes) {
      cx += finiteNumber(graph.getNodeAttribute(id, "x"), target.x);
      cy += finiteNumber(graph.getNodeAttribute(id, "y"), target.y);
    }
    cx /= groupNodes.length;
    cy /= groupNodes.length;
    const shiftX = (target.x - cx) * 0.94;
    const shiftY = (target.y - cy) * 0.94;
    const desiredRadius =
      groupNodes.length <= 8
        ? Math.max(34, Math.sqrt(groupNodes.length) * 18)
        : Math.max(180, Math.sqrt(groupNodes.length) * 58);
    let avgRadius = 0;
    for (const id of groupNodes) {
      const x = finiteNumber(graph.getNodeAttribute(id, "x"), target.x) + shiftX;
      const y = finiteNumber(graph.getNodeAttribute(id, "y"), target.y) + shiftY;
      graph.setNodeAttribute(id, "x", x);
      graph.setNodeAttribute(id, "y", y);
      avgRadius += Math.hypot(x - target.x, y - target.y);
    }
    avgRadius /= groupNodes.length;
    if (avgRadius > 0 && avgRadius < desiredRadius) {
      const scale = Math.min(2.0, desiredRadius / avgRadius);
      for (const id of groupNodes) {
        const x = finiteNumber(graph.getNodeAttribute(id, "x"), target.x);
        const y = finiteNumber(graph.getNodeAttribute(id, "y"), target.y);
        graph.setNodeAttribute(id, "x", target.x + (x - target.x) * scale);
        graph.setNodeAttribute(id, "y", target.y + (y - target.y) * scale);
      }
    }
  });

  const collisionPasses = Math.max(1, Math.min(passes, nodeCount > 2200 ? 2 : 4));
  const cellSize = nodeCount > 2500 ? 104 : 128;
  for (let pass = 0; pass < collisionPasses; pass += 1) {
    const grid = new Map<string, string[]>();
    const cellFor = (x: number, y: number) => {
      const cx = Math.floor(x / cellSize);
      const cy = Math.floor(y / cellSize);
      return `${cx}:${cy}`;
    };

    for (const id of nodes) {
      const x = finiteNumber(graph.getNodeAttribute(id, "x"), 0);
      const y = finiteNumber(graph.getNodeAttribute(id, "y"), 0);
      const key = cellFor(x, y);
      const list = grid.get(key) || [];
      list.push(id);
      grid.set(key, list);
    }

    let moved = false;
    for (const a of nodes) {
      let ax = finiteNumber(graph.getNodeAttribute(a, "x"), 0);
      let ay = finiteNumber(graph.getNodeAttribute(a, "y"), 0);
      const acx = Math.floor(ax / cellSize);
      const acy = Math.floor(ay / cellSize);
      const aCluster = String(graph.getNodeAttribute(a, "brain_cluster_key") || "");
      const aSize = finiteNumber(graph.getNodeAttribute(a, "size"), 5);
      for (let gx = acx - 1; gx <= acx + 1; gx += 1) {
        for (let gy = acy - 1; gy <= acy + 1; gy += 1) {
          const bucket = grid.get(`${gx}:${gy}`);
          if (!bucket) continue;
          for (const b of bucket) {
            if (a >= b) continue;
            let bx = finiteNumber(graph.getNodeAttribute(b, "x"), 0);
            let by = finiteNumber(graph.getNodeAttribute(b, "y"), 0);
            let dx = ax - bx;
            let dy = ay - by;
            let dist = Math.hypot(dx, dy);
            if (!Number.isFinite(dist) || dist < 0.001) {
              const angle = stableUnitFromString(`${a}:${b}:brain-collide`) * Math.PI * 2;
              dx = Math.cos(angle) * 0.001;
              dy = Math.sin(angle) * 0.001;
              dist = 0.001;
            }
            const bCluster = String(graph.getNodeAttribute(b, "brain_cluster_key") || "");
            const bSize = finiteNumber(graph.getNodeAttribute(b, "size"), 5);
            const sameCluster = aCluster === bCluster;
            const padding = sameCluster
              ? nodeCount > 2500
                ? 58
                : 82
              : nodeCount > 2500
                ? 96
                : 122;
            const minDist = aSize + bSize + padding;
            if (dist >= minDist) continue;
            const push = ((minDist - dist) / dist) * 0.5;
            const px = dx * push;
            const py = dy * push;
            ax += px;
            ay += py;
            bx -= px;
            by -= py;
            graph.setNodeAttribute(b, "x", bx);
            graph.setNodeAttribute(b, "y", by);
            moved = true;
          }
        }
      }
      graph.setNodeAttribute(a, "x", ax);
      graph.setNodeAttribute(a, "y", ay);
    }
    if (!moved) break;
  }
}

function nodeScreenRadius(
  graph: Graph<SigmaNodeAttributes, SigmaEdgeAttributes>,
  id: string,
  mode: "brain" | "query",
): number {
  const size = finiteNumber(graph.getNodeAttribute(id, "size"), 5);
  const kind = String(graph.getNodeAttribute(id, "nodeKind") || "");
  const forced = Boolean(graph.getNodeAttribute(id, "forceLabel"));
  if (mode === "query") return Math.max(10, size * (forced ? 2.8 : 2.1));
  if (kind === "Book") return Math.max(6, size * 1.8);
  if (kind === "Document") return Math.max(5, size * 1.35);
  return Math.max(3.5, size * 1.15);
}

function nodeMobility(
  graph: Graph<SigmaNodeAttributes, SigmaEdgeAttributes>,
  id: string,
): number {
  const kind = String(graph.getNodeAttribute(id, "nodeKind") || "");
  if (kind === "Book") return 0.42;
  if (Boolean(graph.getNodeAttribute(id, "forceLabel"))) return 0.58;
  return 1.0;
}

function enforceViewportCollision(
  sigma: Sigma | null,
  graph: Graph<SigmaNodeAttributes, SigmaEdgeAttributes>,
  mode: "brain" | "query",
  passes = 3,
) {
  if (!sigma || mode === "query") return;
  const graphToViewport = (sigma as any).graphToViewport?.bind(sigma);
  const viewportToGraph = (sigma as any).viewportToGraph?.bind(sigma);
  if (typeof graphToViewport !== "function" || typeof viewportToGraph !== "function") return;

  const nodes = graph.nodes().filter((id) => !graph.getNodeAttribute(id, "hidden"));
  if (nodes.length < 2) return;

  const cellSize = 42;
  for (let pass = 0; pass < passes; pass += 1) {
    const points = new Map<string, { x: number; y: number; r: number; mobility: number }>();
    const grid = new Map<string, string[]>();
    const cellFor = (x: number, y: number) => `${Math.floor(x / cellSize)}:${Math.floor(y / cellSize)}`;

    for (const id of nodes) {
      const x = finiteNumber(graph.getNodeAttribute(id, "x"), 0);
      const y = finiteNumber(graph.getNodeAttribute(id, "y"), 0);
      const p = graphToViewport({ x, y });
      if (!p || !Number.isFinite(p.x) || !Number.isFinite(p.y)) continue;
      const point = {
        x: p.x,
        y: p.y,
        r: nodeScreenRadius(graph, id, mode),
        mobility: nodeMobility(graph, id),
      };
      points.set(id, point);
      const key = cellFor(point.x, point.y);
      const bucket = grid.get(key) || [];
      bucket.push(id);
      grid.set(key, bucket);
    }

    let moved = false;
    for (const a of nodes) {
      const pa = points.get(a);
      if (!pa) continue;
      const acx = Math.floor(pa.x / cellSize);
      const acy = Math.floor(pa.y / cellSize);

      for (let gx = acx - 1; gx <= acx + 1; gx += 1) {
        for (let gy = acy - 1; gy <= acy + 1; gy += 1) {
          const bucket = grid.get(`${gx}:${gy}`);
          if (!bucket) continue;

          for (const b of bucket) {
            if (a >= b) continue;
            const pb = points.get(b);
            if (!pb) continue;

            let dx = pa.x - pb.x;
            let dy = pa.y - pb.y;
            let dist = Math.hypot(dx, dy);
            if (!Number.isFinite(dist) || dist < 0.01) {
              const angle = stableUnitFromString(`${a}:${b}:viewport-collide`) * Math.PI * 2;
              dx = Math.cos(angle) * 0.01;
              dy = Math.sin(angle) * 0.01;
              dist = 0.01;
            }

            const minDist = pa.r + pb.r + 3;
            if (dist >= minDist) continue;

            const push = ((minDist - dist) / dist) * 0.56;
            const totalMobility = Math.max(0.01, pa.mobility + pb.mobility);
            const aShare = pb.mobility / totalMobility;
            const bShare = pa.mobility / totalMobility;
            const ax = dx * push * aShare;
            const ay = dy * push * aShare;
            const bx = -dx * push * bShare;
            const by = -dy * push * bShare;

            const aGraph = {
              x: finiteNumber(graph.getNodeAttribute(a, "x"), 0),
              y: finiteNumber(graph.getNodeAttribute(a, "y"), 0),
            };
            const bGraph = {
              x: finiteNumber(graph.getNodeAttribute(b, "x"), 0),
              y: finiteNumber(graph.getNodeAttribute(b, "y"), 0),
            };
            const aNext = viewportToGraph({ x: pa.x + ax, y: pa.y + ay });
            const bNext = viewportToGraph({ x: pb.x + bx, y: pb.y + by });
            if (aNext && Number.isFinite(aNext.x) && Number.isFinite(aNext.y)) {
              graph.setNodeAttribute(a, "x", aGraph.x + (aNext.x - aGraph.x));
              graph.setNodeAttribute(a, "y", aGraph.y + (aNext.y - aGraph.y));
              pa.x += ax;
              pa.y += ay;
              moved = true;
            }
            if (bNext && Number.isFinite(bNext.x) && Number.isFinite(bNext.y)) {
              graph.setNodeAttribute(b, "x", bGraph.x + (bNext.x - bGraph.x));
              graph.setNodeAttribute(b, "y", bGraph.y + (bNext.y - bGraph.y));
              pb.x += bx;
              pb.y += by;
              moved = true;
            }
          }
        }
      }
    }
    if (!moved) break;
  }
}

function settleBrainAtlas(
  graph: Graph<SigmaNodeAttributes, SigmaEdgeAttributes>,
  sigma: Sigma | null,
) {
  enforceBrainClusterSpacing(graph, graph.order > 1800 ? 4 : 6);
  try {
    noverlap.assign(graph, getNoverlapSettings(graph.order, "brain"));
  } catch {
    /* noverlap can throw while graphology is mid-update; spacing still holds */
  }
  enforceBrainClusterSpacing(graph, graph.order > 1800 ? 2 : 3);
  enforceViewportCollision(sigma, graph, "brain", graph.order > 1800 ? 2 : 3);
}

// Cut layout duration roughly 4-5× — settles fast enough on
// modern hardware and matches the user-perceived "load" window.
const getLayoutDuration = (nodeCount: number): number => {
  if (nodeCount > 5000) return 8000;
  if (nodeCount > 1000) return 6000;
  if (nodeCount > 500) return 5000;
  return 4000;
};

// ── Layout position cache ────────────────────────────────────────────────
// Re-opening the same graph used to replay the full FA2 settle (4–8s of
// churn at multi-hundred-file scale) just to re-derive the same shape.
// Final node positions are cached per graph fingerprint (sessionStorage,
// LRU-capped) and restored on the next open: FA2 STILL runs — the motion
// design is unchanged — but it starts from the settled answer and converges
// in under a second. Changed graphs (different node set) miss the cache and
// animate exactly as before. Drag-release re-settles never restore (they
// pass a duration override), so user-placed nodes stay where dropped.
// localStorage (not sessionStorage): settled layouts survive browser
// restarts, so the graph opens pre-settled on EVERY visit, not just within
// one tab session. Entries are keyed by graph fingerprint, so a changed
// corpus misses the cache and animates a fresh layout as before.
const POS_CACHE_KEY = "polymath.graph.positions.v13";
const POS_CACHE_MAX_ENTRIES = 8;
const RESTORED_SETTLE_MS = 800;

const graphFingerprint = (
  graph: Graph<SigmaNodeAttributes, SigmaEdgeAttributes>,
): string => {
  const keys = graph.nodes().sort();
  let h = 5381;
  for (const k of keys) {
    for (let i = 0; i < k.length; i++) h = ((h << 5) + h + k.charCodeAt(i)) | 0;
    h = ((h << 5) + h + 124) | 0; // key separator
  }
  return `${graph.order}:${graph.size}:${h}`;
};

type PositionCache = Record<
  string,
  { t: number; pos: Record<string, [number, number]> }
>;

const readPositionCache = (): PositionCache => {
  try {
    return JSON.parse(localStorage.getItem(POS_CACHE_KEY) || "{}");
  } catch {
    return {};
  }
};

const saveLayoutPositions = (
  graph: Graph<SigmaNodeAttributes, SigmaEdgeAttributes>,
): void => {
  try {
    const cache = readPositionCache();
    const pos: Record<string, [number, number]> = {};
    graph.forEachNode((n, a) => {
      pos[n] = [a.x as number, a.y as number];
    });
    cache[graphFingerprint(graph)] = { t: Date.now(), pos };
    const entries = Object.entries(cache)
      .sort((a, b) => b[1].t - a[1].t)
      .slice(0, POS_CACHE_MAX_ENTRIES);
    localStorage.setItem(POS_CACHE_KEY, JSON.stringify(Object.fromEntries(entries)));
  } catch {
    /* quota / serialization — the cache is best-effort */
  }
};

const restoreLayoutPositions = (
  graph: Graph<SigmaNodeAttributes, SigmaEdgeAttributes>,
): boolean => {
  try {
    const hit = readPositionCache()[graphFingerprint(graph)];
    if (!hit) return false;
    let applied = 0;
    graph.forEachNode((n) => {
      const p = hit.pos[n];
      if (p) {
        graph.setNodeAttribute(n, "x", p[0]);
        graph.setNodeAttribute(n, "y", p[1]);
        applied++;
      }
    });
    return applied === graph.order && applied > 0;
  } catch {
    return false;
  }
};

// ── Hook ─────────────────────────────────────────────────────────────────

export const useSigma = (options: UseSigmaOptions = {}): UseSigmaReturn => {
  const containerRef = useRef<HTMLDivElement>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const graphRef = useRef<Graph<
    SigmaNodeAttributes,
    SigmaEdgeAttributes
  > | null>(null);
  const layoutRef = useRef<FA2Layout | null>(null);
  const selectedNodeRef = useRef<string | null>(null);
  const highlightedRef = useRef<Set<string>>(new Set());
  const layoutTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const draggedRef = useRef<string | null>(null);
  const isDraggingRef = useRef(false);
  const optionsRef = useRef(options);
  const ambientSyncRef = useRef<(() => void) | null>(null);
  const [isLayoutRunning, setIsLayoutRunning] = useState(false);
  const [selectedNode, setSelectedNodeState] = useState<string | null>(null);

  // Keep options live without re-binding sigma on each render.
  useEffect(() => {
    optionsRef.current = options;
    highlightedRef.current = options.highlightedNodeIds || new Set();
    sigmaRef.current?.refresh();
  }, [options]);

  const setSelectedNode = useCallback((nodeId: string | null) => {
    selectedNodeRef.current = nodeId;
    setSelectedNodeState(nodeId);
    const sigma = sigmaRef.current;
    if (!sigma) return;
    // Tiny camera nudge to force edge re-render (Sigma edge cache workaround
    // — verbatim from GitNexus useSigma).
    const camera = sigma.getCamera();
    const currentRatio = camera.ratio;
    camera.animate({ ratio: currentRatio * 1.0001 }, { duration: 50 });
    sigma.refresh();
  }, []);

  // Initialize Sigma ONCE — reducers + hover renderer + event wiring all
  // captured at construction time.
  useEffect(() => {
    if (!containerRef.current) return;

    const graph = new Graph<SigmaNodeAttributes, SigmaEdgeAttributes>();
    graphRef.current = graph;

    const sigma = new Sigma(graph, containerRef.current, {
      renderLabels: true,
      labelFont: "Inter, JetBrains Mono, ui-sans-serif, system-ui, sans-serif",
      labelSize: 12,
      labelWeight: "500",
      labelColor: { color: "#e4e4ed" },
      labelRenderedSizeThreshold: 8,
      labelDensity: 0.1,
      labelGridCellSize: 70,

      defaultNodeColor: "#6b7280",
      defaultEdgeColor: "#2a2a3a",
      nodeProgramClasses: {
        bookGlow: BookGlowProgram,
      },

      defaultEdgeType: "curved",
      edgeProgramClasses: {
        curved: EdgeCurveProgram,
      },

      // Custom hover renderer — premium redesign (Pt 7d): pill with the
      // node's color as a 1px accent border, a soft glow ring around the
      // node, and the label in slate-100 on a tinted chip. Verbatim
      // signature from GitNexus so it stays compatible with sigma's
      // internal hover hooks.
      defaultDrawNodeHover: (context: any, data: any, settings: any) => {
        const label = data.label;
        if (!label) return;
        const size = settings.labelSize || 12;
        const font =
          settings.labelFont ||
          "Inter, JetBrains Mono, ui-sans-serif, system-ui, sans-serif";
        const weight = settings.labelWeight || "500";
        context.font = `${weight} ${size}px ${font}`;
        const textWidth = context.measureText(label).width;
        const nodeSize = data.size || 8;
        const x = data.x;
        const y = data.y - nodeSize - 10;
        const paddingX = 8;
        const paddingY = 5;
        const height = size + paddingY * 2;
        const width = textWidth + paddingX * 2;
        const radius = 5;
        // Tinted chip background — same substrate tone as the canvas
        // chrome so the hover pill reads as part of the UI, not a stray
        // tooltip.
        context.fillStyle = "#11141b";
        context.beginPath();
        if (typeof context.roundRect === "function") {
          context.roundRect(
            x - width / 2,
            y - height / 2,
            width,
            height,
            radius,
          );
        } else {
          context.rect(x - width / 2, y - height / 2, width, height);
        }
        context.fill();
        // 1px accent border matching the node color.
        context.strokeStyle = data.color || "#6366f1";
        context.lineWidth = 1;
        context.stroke();
        // Label text — slate-100.
        context.fillStyle = "#f1f5f9";
        context.textAlign = "center";
        context.textBaseline = "middle";
        context.fillText(label, x, y);
        // Soft glow ring around the node — three concentric strokes so the
        // hover reads even on densely-saturated regions of the canvas.
        const glowColor = data.color || "#6366f1";
        context.beginPath();
        context.arc(data.x, data.y, nodeSize + 3, 0, Math.PI * 2);
        context.strokeStyle = glowColor;
        context.lineWidth = 1.5;
        context.globalAlpha = 0.85;
        context.stroke();
        context.globalAlpha = 0.35;
        context.beginPath();
        context.arc(data.x, data.y, nodeSize + 7, 0, Math.PI * 2);
        context.strokeStyle = glowColor;
        context.lineWidth = 1;
        context.stroke();
        context.globalAlpha = 1;
      },

      minCameraRatio: 0.002,
      maxCameraRatio: 50,
      hideEdgesOnMove: true,
      zIndex: true,

      nodeReducer: (node: string, data: any) => {
        const res = { ...data };
        if (data.hidden) {
          res.hidden = true;
          return res;
        }
        const sel = selectedNodeRef.current;
        const highlighted = highlightedRef.current;
        const hasHighlights = highlighted.size > 0;
        const isQueryHighlighted = highlighted.has(node);

        if (hasHighlights && !sel) {
          if (isQueryHighlighted) {
            res.color = "#06b6d4";
            res.size = (data.size || 8) * 1.6;
            res.zIndex = 2;
            res.highlighted = true;
          } else {
            res.color = dimColor(data.color, 0.2);
            res.size = (data.size || 8) * 0.5;
            res.zIndex = 0;
          }
          return res;
        }

        if (sel) {
          const g = graphRef.current;
          if (g) {
            const isSelected = node === sel;
            const isNeighbor = g.hasEdge(node, sel) || g.hasEdge(sel, node);
            if (isSelected) {
              res.color = data.color;
              // 1.25 keeps the highlight readable without overwhelming
              // neighbors on the new substrate.
              res.size = (data.size || 8) * 1.25;
              res.zIndex = 2;
              res.highlighted = true;
              res.forceLabel = true;
            } else if (isNeighbor) {
              res.color = data.color;
              // 1.1 nudges neighbors just enough to register visually
              // without competing for attention.
              res.size = (data.size || 8) * 1.1;
              res.zIndex = 1;
              res.forceLabel = true;
            } else {
              res.color = dimColor(data.color, 0.25);
              res.size = (data.size || 8) * 0.6;
              res.zIndex = 0;
            }
          }
          return res;
        }

        // No selection — boost seeds and hubs.
        if (data.isSeed) {
          const isQueryGraph = (optionsRef.current.layoutMode ?? "brain") === "query";
          res.color = brightenColor(data.color, 1.25);
          res.size = (data.size || 8) * 1.25;
          res.zIndex = 2;
          res.forceLabel = Boolean(data.forceLabel) || isQueryGraph;
        } else if (data.isHub) {
          res.color = brightenColor(data.color, 1.1);
          res.zIndex = 1;
        }
        return res;
      },

      edgeReducer: (edge: string, data: any) => {
        const res = { ...data };
        const sel = selectedNodeRef.current;
        const highlighted = highlightedRef.current;
        const hasHighlights = highlighted.size > 0;
        if (!sel) {
          res.label = undefined;
          if (data.visual_scaffold) {
            res.size = Math.max(0.01, (data.size || 0.02) * 0.35);
            res.color = data.color || "#172033";
            res.zIndex = 0;
          }
        }

        if (hasHighlights && !sel) {
          const g = graphRef.current;
          if (g) {
            const [source, target] = g.extremities(edge);
            const both = highlighted.has(source) && highlighted.has(target);
            const one = highlighted.has(source) || highlighted.has(target);
            if (both) {
              res.color = "#06b6d4";
              res.size = Math.max(2, (data.size || 1) * 3);
              res.zIndex = 2;
            } else if (one) {
              res.color = dimColor("#06b6d4", 0.4);
              res.size = 1;
              res.zIndex = 1;
            } else {
              res.color = dimColor(data.color, 0.08);
              res.size = 0.2;
              res.zIndex = 0;
            }
          }
          return res;
        }

        if (sel) {
          const g = graphRef.current;
          if (g) {
            const [source, target] = g.extremities(edge);
            const isConnected = source === sel || target === sel;
            if (isConnected) {
              res.color = brightenColor(data.color, 1.5);
              res.size = Math.max(3, (data.size || 1) * 4);
              res.zIndex = 2;
            } else {
              res.color = dimColor(data.color, 0.1);
              res.size = 0.3;
              res.zIndex = 0;
            }
          }
        }
        return res;
      },
    });

    sigmaRef.current = sigma;

    // ── Semantic zoom (Pt 3 polish) ────────────────────────────────────
    // At zoom-out only forceLabel anchors (Brain View top-N books) show
    // their label; at deep zoom-out labels disappear entirely; at zoom-in
    // the full density returns. Debounced via one rAF so it doesn't fight
    // sigma's own redraw loop.
    let rafScheduled = false;
    const applySemanticZoom = () => {
      rafScheduled = false;
      const s = sigmaRef.current;
      const g = graphRef.current;
      if (!s || !g) return;
      const ratio = s.getCamera().ratio;
      const nodeCount = g.order;
      const layoutMode = optionsRef.current.layoutMode ?? "brain";
      const isQueryGraph = layoutMode === "query";
      const isLargeGraph = nodeCount > (isQueryGraph ? 500 : 800);
      const isHugeGraph = nodeCount > (isQueryGraph ? 1800 : 3000);
      // Camera tier: closer in = lower ratio. Defaults Sigma uses ~1.0.
      const zoomedFar = ratio >= 4;
      const zoomedMid = ratio >= 1.5 && ratio < 4;
      // Overview behaves like Obsidian: labels are interaction-first.
      // Keep label rendering enabled so `forceLabel` still works for
      // selected nodes and drill chunks, but make normal label density
      // effectively zero in brain mode.
      const renderLabels = isQueryGraph ? !zoomedFar : true;
      const labelDensity =
        isQueryGraph
          ? zoomedMid || isLargeGraph
            ? isHugeGraph
              ? 0.006
              : 0.012
            : 0.018
          : 0.001;
      const labelThreshold =
        isQueryGraph
          ? zoomedMid || isLargeGraph
            ? isHugeGraph
              ? 19
              : 16
            : 13
          : 999;
      try {
        s.setSetting("renderLabels", renderLabels);
        s.setSetting("labelDensity", labelDensity);
        s.setSetting("labelRenderedSizeThreshold", labelThreshold);
        s.setSetting("labelSize", isQueryGraph ? 12 : 9.5);
        s.setSetting("labelWeight", isQueryGraph ? "500" : "450");
        s.setSetting("labelColor", {
          color: isQueryGraph ? "#e4e4ed" : "#aeb8c8",
        });
      } catch {
        /* setSetting can throw mid-frame — ignore */
      }
    };
    const onCameraUpdated = () => {
      if (rafScheduled) return;
      rafScheduled = true;
      requestAnimationFrame(applySemanticZoom);
    };
    const camera = sigma.getCamera();
    camera.on("updated", onCameraUpdated);

    sigma.on("clickNode", ({ node }: any) => {
      // Suppress click-as-select when this came at the tail of a drag.
      if (isDraggingRef.current) {
        isDraggingRef.current = false;
        return;
      }
      setSelectedNode(node);
      optionsRef.current.onNodeClick?.(node);
    });
    sigma.on("clickStage", () => {
      setSelectedNode(null);
      optionsRef.current.onStageClick?.();
    });
    sigma.on("doubleClickNode", ({ node, event }: any) => {
      event?.preventSigmaDefault?.();
      optionsRef.current.onDoubleClickNode?.(node);
    });
    sigma.on("enterNode", ({ node }: any) => {
      optionsRef.current.onNodeHover?.(node);
      if (containerRef.current) containerRef.current.style.cursor = "pointer";
    });
    sigma.on("leaveNode", () => {
      optionsRef.current.onNodeHover?.(null);
      if (containerRef.current) containerRef.current.style.cursor = "grab";
    });

    // Node dragging.
    sigma.on("downNode", ({ node, event }: any) => {
      draggedRef.current = node;
      isDraggingRef.current = false;
      const g = graphRef.current;
      if (g && g.hasNode(node)) {
        g.setNodeAttribute(node, "highlighted", true);
      }
      event?.preventSigmaDefault?.();
    });
    const mouseCaptor = sigma.getMouseCaptor();
    mouseCaptor.on("mousemovebody", (e: any) => {
      const dragged = draggedRef.current;
      if (!dragged) return;
      // Pt 3 polish: on the FIRST move of a drag, stop the FA2 layout
      // worker so physics doesn't fight the cursor (rubber-banding). On
      // release we leave the layout off so the user's position sticks —
      // they can hit "Run layout" to re-settle if they want to.
      if (!isDraggingRef.current) {
        layoutRef.current?.stop();
        layoutRef.current = null;
        if (layoutTimeoutRef.current) {
          clearTimeout(layoutTimeoutRef.current);
          layoutTimeoutRef.current = null;
        }
        setIsLayoutRunning(false);
      }
      isDraggingRef.current = true;
      const sigmaInst = sigmaRef.current;
      const g = graphRef.current;
      if (!sigmaInst || !g || !g.hasNode(dragged)) return;
      const pos = sigmaInst.viewportToGraph({ x: e.x, y: e.y });
      g.setNodeAttribute(dragged, "x", pos.x);
      g.setNodeAttribute(dragged, "y", pos.y);
      e.preventSigmaDefault();
      e.original.preventDefault();
      e.original.stopPropagation();
    });
    const release = () => {
      const dragged = draggedRef.current;
      if (dragged) {
        const g = graphRef.current;
        if (g && g.hasNode(dragged)) {
          g.removeNodeAttribute(dragged, "highlighted");
        }
      }
      const wasDragging = isDraggingRef.current;
      draggedRef.current = null;
      isDraggingRef.current = false;
      const layoutMode = optionsRef.current.layoutMode ?? "brain";
      const graph = graphRef.current;
      if (layoutMode === "query" && graph) {
        enforceQueryMinimumSpacing(graph, 8);
        sigmaRef.current?.refresh();
      }
      // Pt 6: settle-restart-after-drag. When the user opts in, restart
      // FA2 for ~5s after a real drag release so neighbors re-arrange
      // around the new node position. Default OFF so position sticks.
      if (wasDragging && optionsRef.current.settleAfterDrag) {
        const g = graphRef.current;
        if (g && g.order > 0 && !layoutRef.current) {
          runLayoutForRef.current?.(g, 5000);
        }
      }
    };
    mouseCaptor.on("mouseup", release);
    mouseCaptor.on("mouseleave", release);

    return () => {
      if (layoutTimeoutRef.current) clearTimeout(layoutTimeoutRef.current);
      layoutRef.current?.kill();
      try {
        camera.off("updated", onCameraUpdated);
      } catch {
        /* sigma may already be torn down */
      }
      sigma.kill();
      sigmaRef.current = null;
      graphRef.current = null;
    };
  }, [setSelectedNode]);

  // Controlled ambient graph motion: BookGlowProgram uses a time uniform, so
  // settled graphs need a low-cadence render pulse. Guard it carefully so the
  // effect stays quiet on reduced-motion, hidden tabs, and offscreen canvases.
  useEffect(() => {
    const container = containerRef.current;
    if (!container || typeof window === "undefined") return;

    const media = window.matchMedia("(prefers-reduced-motion: reduce)");
    let onscreen = true;
    let timer: number | null = null;

    const shouldAnimate = () =>
      !media.matches &&
      document.visibilityState === "visible" &&
      onscreen &&
      Boolean(graphRef.current?.order);

    const stop = () => {
      if (timer == null) return;
      window.clearInterval(timer);
      timer = null;
    };

    const sync = () => {
      if (!shouldAnimate()) {
        stop();
        return;
      }
      if (timer != null) return;
      timer = window.setInterval(() => {
        if (!shouldAnimate()) {
          stop();
          return;
        }
        sigmaRef.current?.scheduleRender();
      }, Math.round(1000 / motionTokens.ambientGraphFps));
    };
    ambientSyncRef.current = sync;

    const visibilityHandler = () => sync();
    const mediaHandler = () => sync();
    document.addEventListener("visibilitychange", visibilityHandler);
    media.addEventListener?.("change", mediaHandler);

    const observer = new IntersectionObserver(
      ([entry]) => {
        onscreen = Boolean(entry?.isIntersecting);
        sync();
      },
      { threshold: 0.05 },
    );
    observer.observe(container);
    sync();

    return () => {
      ambientSyncRef.current = null;
      stop();
      observer.disconnect();
      document.removeEventListener("visibilitychange", visibilityHandler);
      media.removeEventListener?.("change", mediaHandler);
    };
  }, []);

  // Run ForceAtlas2 layout — verbatim from GitNexus useSigma.
  // Pt 6: refactored to take an optional duration override so the
  // settle-after-drag handler can request a short 5s settle while the
  // initial-load callsite uses the tier-based default duration.
  const runLayoutFor = useCallback(
    (
      graph: Graph<SigmaNodeAttributes, SigmaEdgeAttributes>,
      durationOverrideMs?: number,
    ) => {
      const nodeCount = graph.order;
      if (nodeCount === 0) return;

      if (layoutRef.current) {
        layoutRef.current.kill();
        layoutRef.current = null;
      }
      if (layoutTimeoutRef.current) {
        clearTimeout(layoutTimeoutRef.current);
        layoutTimeoutRef.current = null;
      }

      const inferredSettings = forceAtlas2.inferSettings(graph);
      const layoutMode = optionsRef.current.layoutMode ?? "brain";
      if (layoutMode !== "query") {
        settleBrainAtlas(graph, sigmaRef.current);
        saveLayoutPositions(graph);
        sigmaRef.current?.refresh();
        setIsLayoutRunning(false);
        return;
      }
      const customSettings = getFA2Settings(
        nodeCount,
        layoutMode,
        optionsRef.current.queryFingerprint,
      );
      const settings = { ...inferredSettings, ...customSettings };

      // Position-cache restore — only for fresh loads (no duration override;
      // overrides come from drag-release re-settles, where snapping nodes
      // back to cached spots would undo the user's drag).
      const restored =
        durationOverrideMs == null && restoreLayoutPositions(graph);
      const layout = new FA2Layout(graph, { settings });
      layoutRef.current = layout;
      layout.start();
      setIsLayoutRunning(true);

      const duration =
        durationOverrideMs ??
        (restored ? RESTORED_SETTLE_MS : getLayoutDuration(nodeCount));
      layoutTimeoutRef.current = setTimeout(() => {
        if (layoutRef.current) {
          layoutRef.current.stop();
          layoutRef.current = null;
          try {
            noverlap.assign(
              graph,
              getNoverlapSettings(graph.order, layoutMode),
            );
            if (layoutMode === "query") {
              enforceQueryMinimumSpacing(graph, 12);
            } else {
              enforceBrainClusterSpacing(graph, nodeCount > 1800 ? 3 : 5);
              enforceViewportCollision(
                sigmaRef.current,
                graph,
                layoutMode,
                nodeCount > 1800 ? 2 : 3,
              );
            }
          } catch {
            /* ignore */
          }
          saveLayoutPositions(graph);
          sigmaRef.current?.refresh();
          setIsLayoutRunning(false);
        }
      }, duration);
    },
    [],
  );

  // Back-compat alias — tier-based default duration when no override given.
  const runLayout = useCallback(
    (graph: Graph<SigmaNodeAttributes, SigmaEdgeAttributes>) => {
      runLayoutFor(graph);
    },
    [runLayoutFor],
  );

  // Stash the latest runLayoutFor in a ref so the drag-release handler
  // (created inside the init useEffect) can call it without going stale.
  const runLayoutForRef = useRef<typeof runLayoutFor | null>(null);
  useEffect(() => {
    runLayoutForRef.current = runLayoutFor;
  }, [runLayoutFor]);

  const setGraph = useCallback(
    (newGraph: Graph<SigmaNodeAttributes, SigmaEdgeAttributes>) => {
      const sigma = sigmaRef.current;
      if (!sigma) return;
      if (layoutRef.current) {
        layoutRef.current.kill();
        layoutRef.current = null;
      }
      if (layoutTimeoutRef.current) {
        clearTimeout(layoutTimeoutRef.current);
        layoutTimeoutRef.current = null;
      }
      graphRef.current = newGraph;
      // ── LOD: gate labels by route ───────────────────────────────
      // Query graphs keep curated labels. Brain/corpus overview keeps
      // labels interaction-first: hover/selection/drill chunks only.
      const nodeCount = newGraph.order;
      const layoutMode = optionsRef.current.layoutMode ?? "brain";
      const isQueryGraph = layoutMode === "query";
      const isLargeGraph = nodeCount > (isQueryGraph ? 500 : 800);
      const isHugeGraph = nodeCount > (isQueryGraph ? 1800 : 3000);
      try {
        sigma.setSetting("renderLabels", true);
        sigma.setSetting("hideEdgesOnMove", !isQueryGraph);
        sigma.setSetting("labelSize", isQueryGraph ? 12 : 9.5);
        sigma.setSetting("labelWeight", isQueryGraph ? "500" : "450");
        sigma.setSetting("labelColor", {
          color: isQueryGraph ? "#e4e4ed" : "#aeb8c8",
        });
        sigma.setSetting(
          "labelDensity",
          isQueryGraph
            ? isHugeGraph
              ? 0.006
              : isLargeGraph
                ? 0.012
                : 0.018
            : 0.001,
        );
        sigma.setSetting(
          "labelRenderedSizeThreshold",
          isQueryGraph
            ? isHugeGraph
              ? 19
              : isLargeGraph
                ? 16
                : 13
            : 999,
        );
      } catch {
        /* setSetting may throw if the renderer is mid-frame — ignore */
      }
      sigma.setGraph(newGraph);
      if (isQueryGraph) {
        try {
          noverlap.assign(newGraph, getNoverlapSettings(nodeCount, "query"));
          enforceQueryMinimumSpacing(newGraph, 10);
        } catch {
          /* ignore */
        }
      }
      setSelectedNode(null);
      if (isQueryGraph) {
        sigma.refresh();
      } else {
        runLayout(newGraph);
      }
      ambientSyncRef.current?.();
      sigma.getCamera().animatedReset({ duration: 500 });
      if (!isQueryGraph) {
        window.setTimeout(() => {
          enforceViewportCollision(sigma, newGraph, "brain", newGraph.order > 1800 ? 2 : 3);
          sigma.refresh();
        }, 560);
      }
    },
    [runLayout, setSelectedNode],
  );

  const focusNode = useCallback((nodeId: string) => {
    const sigma = sigmaRef.current;
    const graph = graphRef.current;
    if (!sigma || !graph || !graph.hasNode(nodeId)) return;
    const alreadySelected = selectedNodeRef.current === nodeId;
    selectedNodeRef.current = nodeId;
    setSelectedNodeState(nodeId);
    if (!alreadySelected) {
      const nodeAttrs = graph.getNodeAttributes(nodeId);
      sigma
        .getCamera()
        .animate(
          { x: nodeAttrs.x, y: nodeAttrs.y, ratio: 0.15 },
          { duration: 400 },
        );
    }
    sigma.refresh();
  }, []);

  const zoomIn = useCallback(() => {
    sigmaRef.current?.getCamera().animatedZoom({ duration: 200 });
  }, []);

  const zoomOut = useCallback(() => {
    sigmaRef.current?.getCamera().animatedUnzoom({ duration: 200 });
  }, []);

  const resetZoom = useCallback(() => {
    sigmaRef.current?.getCamera().animatedReset({ duration: 300 });
    setSelectedNode(null);
    window.setTimeout(() => {
      const graph = graphRef.current;
      const sigma = sigmaRef.current;
      const layoutMode = optionsRef.current.layoutMode ?? "brain";
      if (!graph || !sigma || layoutMode === "query") return;
      enforceViewportCollision(sigma, graph, layoutMode, graph.order > 1800 ? 2 : 3);
      sigma.refresh();
    }, 340);
  }, [setSelectedNode]);

  const startLayout = useCallback(() => {
    const graph = graphRef.current;
    if (!graph || graph.order === 0) return;
    runLayout(graph);
  }, [runLayout]);

  const stopLayout = useCallback(() => {
    if (layoutTimeoutRef.current) {
      clearTimeout(layoutTimeoutRef.current);
      layoutTimeoutRef.current = null;
    }
    if (layoutRef.current) {
      layoutRef.current.stop();
      layoutRef.current = null;
      const graph = graphRef.current;
      if (graph) {
        try {
          const layoutMode = optionsRef.current.layoutMode ?? "brain";
          noverlap.assign(
            graph,
            getNoverlapSettings(graph.order, layoutMode),
          );
          if (layoutMode === "query") {
            enforceQueryMinimumSpacing(graph, 10);
          } else {
            enforceBrainClusterSpacing(graph, graph.order > 1800 ? 3 : 5);
            enforceViewportCollision(
              sigmaRef.current,
              graph,
              layoutMode,
              graph.order > 1800 ? 2 : 3,
            );
          }
        } catch {
          /* ignore */
        }
        sigmaRef.current?.refresh();
      }
      setIsLayoutRunning(false);
    }
  }, []);

  const refreshHighlights = useCallback(() => {
    sigmaRef.current?.refresh();
  }, []);

  return {
    containerRef,
    sigmaRef,
    setGraph,
    zoomIn,
    zoomOut,
    resetZoom,
    focusNode,
    isLayoutRunning,
    startLayout,
    stopLayout,
    selectedNode,
    setSelectedNode,
    refreshHighlights,
  };
};
