/**
 * GalaxyBackground — a single <canvas> layered behind sigma's canvas that
 * paints three things synced to sigma's camera transform:
 *
 *   1. **Particle dust** — ~200 sparse random dim dots, painted ONCE in graph
 *      coordinates so they pan/zoom with the canvas like real distant stars
 *      (they're stored in graph-space and reprojected through the camera each
 *      frame; constant cost, never recomputed).
 *
 *   2. **Family-centroid nebulae** — translucent radial-gradient circles
 *      centered on each `dominant_family` group's centroid. Centroids +
 *      radii are recomputed ONLY after FA2 stops settling (we listen for
 *      sigma's `afterRender` event but only refresh the centroid cache
 *      when the layout-running state flips false). Per-frame cost is just
 *      drawing N radial-gradient circles where N = number of families.
 *
 *   3. **Book glow halos** — a soft radial-gradient halo painted at each
 *      Book anchor's screen position every frame (cheap O(visible-books)
 *      pass, gradient fill on 2D canvas — no WebGL shader needed). Gives
 *      the "stars in the night sky" feel without a custom NodeProgram.
 *
 * The canvas is `position: absolute` filling the sigma container, with
 * `pointer-events: none` so all interaction passes through to sigma.
 *
 * Pt 6 polish.
 */

import { useEffect, useRef } from "react";
import type Sigma from "sigma";
import type Graph from "graphology";

import { colorForFamily } from "../../lib/sigma-constants";

interface GalaxyBackgroundProps {
  sigmaRef: React.RefObject<Sigma | null>;
  /** When this flips false→true (i.e. layout just settled), we re-snapshot
   *  family centroids. While true (settling), we keep the previous snapshot
   *  to avoid recomputing every frame. */
  isLayoutRunning: boolean;
}

interface DustParticle {
  x: number;  // graph-space coordinates so dust pans/zooms with the canvas
  y: number;
  size: number;
  alpha: number;
}

interface NebulaPatch {
  cx: number;   // graph-space centroid
  cy: number;
  radius: number; // graph-space radius covering the family's books
  color: string;  // family hex
  count: number;  // member count (drives intensity)
}

const DUST_COUNT = 220;
const DUST_SPREAD = 4000; // graph-coord half-width; sigma's golden-angle layout
                          //   spreads books over ~+/-500 so 4000 covers a comfy
                          //   margin even after the user pans far.

function makeDust(): DustParticle[] {
  const out: DustParticle[] = [];
  for (let i = 0; i < DUST_COUNT; i++) {
    out.push({
      x: (Math.random() * 2 - 1) * DUST_SPREAD,
      y: (Math.random() * 2 - 1) * DUST_SPREAD,
      size: Math.random() * 1.3 + 0.4,
      alpha: Math.random() * 0.45 + 0.08,
    });
  }
  return out;
}

export function GalaxyBackground({
  sigmaRef,
  isLayoutRunning,
}: GalaxyBackgroundProps) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const dustRef = useRef<DustParticle[]>(makeDust());
  const nebulaeRef = useRef<NebulaPatch[]>([]);
  // Cached so we can skip the heavy centroid recompute on every frame —
  // only refresh when layout finishes settling.
  const wasRunningRef = useRef<boolean>(isLayoutRunning);

  // Recompute centroids when layout transitions running → idle, or on initial
  // mount when there's a graph already present.
  useEffect(() => {
    const transitionedToIdle = wasRunningRef.current && !isLayoutRunning;
    const initialCalmMount = !wasRunningRef.current && !isLayoutRunning;
    wasRunningRef.current = isLayoutRunning;
    if (!transitionedToIdle && !initialCalmMount) return;
    const sigma = sigmaRef.current;
    if (!sigma) return;
    const g = (sigma as any).getGraph?.() as Graph | undefined;
    if (!g || g.order === 0) {
      nebulaeRef.current = [];
      return;
    }
    nebulaeRef.current = computeFamilyNebulae(g);
  }, [isLayoutRunning, sigmaRef]);

  // Attach a per-frame painter to sigma's afterRender event so the dust +
  // nebulae + book halos repaint in lockstep with the main canvas.
  useEffect(() => {
    const sigma = sigmaRef.current;
    const canvas = canvasRef.current;
    if (!sigma || !canvas) return;

    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    // Match the sigma canvas size whenever sigma resizes.
    const resize = () => {
      const dim = (sigma as any).getDimensions?.();
      if (!dim) return;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = dim.width * dpr;
      canvas.height = dim.height * dpr;
      canvas.style.width = `${dim.width}px`;
      canvas.style.height = `${dim.height}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    };
    resize();
    sigma.on("resize", resize);

    const paint = () => {
      const dim = (sigma as any).getDimensions?.();
      if (!dim) return;
      ctx.clearRect(0, 0, dim.width, dim.height);

      // ── 1. Family nebulae (translucent radial gradients) ────────────────
      for (const n of nebulaeRef.current) {
        const screenC = sigma.graphToViewport({ x: n.cx, y: n.cy });
        // Convert graph-space radius to screen-space by reprojecting an
        // offset point. graphToViewport handles camera + scaling.
        const edge = sigma.graphToViewport({ x: n.cx + n.radius, y: n.cy });
        const r = Math.max(40, Math.hypot(edge.x - screenC.x, edge.y - screenC.y));
        const grad = ctx.createRadialGradient(
          screenC.x,
          screenC.y,
          0,
          screenC.x,
          screenC.y,
          r,
        );
        // Soft cloud — heart bright, edges fade. Alpha scales with member count.
        const intensity = Math.min(0.18, 0.04 + n.count * 0.012);
        grad.addColorStop(0, hexWithAlpha(n.color, intensity));
        grad.addColorStop(0.5, hexWithAlpha(n.color, intensity * 0.45));
        grad.addColorStop(1, hexWithAlpha(n.color, 0));
        ctx.fillStyle = grad;
        ctx.beginPath();
        ctx.arc(screenC.x, screenC.y, r, 0, Math.PI * 2);
        ctx.fill();
      }

      // ── 2. Dust particles (constant set of points reprojected each frame) ─
      const ratio = sigma.getCamera().ratio;
      const dustAlphaScale = Math.max(0.35, Math.min(1, 1.8 / ratio)); // dim when zoomed out
      for (const d of dustRef.current) {
        const p = sigma.graphToViewport({ x: d.x, y: d.y });
        if (p.x < -10 || p.x > dim.width + 10) continue;
        if (p.y < -10 || p.y > dim.height + 10) continue;
        ctx.fillStyle = `rgba(255, 255, 255, ${(d.alpha * dustAlphaScale).toFixed(3)})`;
        ctx.beginPath();
        ctx.arc(p.x, p.y, d.size, 0, Math.PI * 2);
        ctx.fill();
      }

      // ── 3. Book glow halos (Pt 6 cheap bloom) ───────────────────────────
      // For each Book anchor we draw a soft radial-gradient halo around its
      // screen position. Cost: O(visible books) gradient fills per frame.
      // Falls back gracefully when the graph is empty.
      const g = (sigma as any).getGraph?.() as Graph | undefined;
      if (g && g.order > 0) {
        g.forEachNode((id, attrs: any) => {
          if (!isBookAnchor(id, attrs)) return;
          const p = sigma.graphToViewport({ x: attrs.x, y: attrs.y });
          if (p.x < -40 || p.x > dim.width + 40) return;
          if (p.y < -40 || p.y > dim.height + 40) return;
          const baseSize = Number(attrs.size) || 7;
          // Halo radius grows with node size; sigma's actual rendered size
          // is roughly `size * scale-factor`, but the camera ratio already
          // accounts for zoom so we just lean on baseSize.
          const haloR = baseSize * 3.2;
          const color = String(attrs.color || "#f59e0b");
          const grad = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, haloR);
          grad.addColorStop(0, hexWithAlpha(color, 0.55));
          grad.addColorStop(0.4, hexWithAlpha(color, 0.18));
          grad.addColorStop(1, hexWithAlpha(color, 0));
          ctx.fillStyle = grad;
          ctx.beginPath();
          ctx.arc(p.x, p.y, haloR, 0, Math.PI * 2);
          ctx.fill();
        });
      }
    };

    sigma.on("afterRender", paint);
    paint();

    return () => {
      sigma.off("resize", resize);
      sigma.off("afterRender", paint);
    };
  }, [sigmaRef]);

  return (
    <canvas
      ref={canvasRef}
      className="pointer-events-none absolute inset-0 z-0"
    />
  );
}

// ── helpers ──────────────────────────────────────────────────────────────

function isBookAnchor(id: string, attrs: any): boolean {
  if (typeof id === "string" && id.startsWith("book:")) return true;
  if (attrs?.is_cluster_anchor) return true;
  if (attrs?.kind === "book") return true;
  if (attrs?.nodeKind === "Book") return true;
  return false;
}

/** Convert "#rrggbb" → "rgba(r,g,b,a)". Accepts rgba() input pass-through. */
function hexWithAlpha(hex: string, alpha: number): string {
  if (hex.startsWith("rgba")) return hex; // already rgba
  const m = /^#?([a-f\d]{2})([a-f\d]{2})([a-f\d]{2})$/i.exec(hex);
  if (!m) return `rgba(150,150,150,${alpha.toFixed(3)})`;
  const r = parseInt(m[1], 16);
  const g = parseInt(m[2], 16);
  const b = parseInt(m[3], 16);
  return `rgba(${r}, ${g}, ${b}, ${alpha.toFixed(3)})`;
}

/** Group Book anchors by dominant_family, compute per-family centroid +
 *  enclosing radius. Returns a list of nebula patches in graph-space.
 *  Only Books with a non-null dominant_family get grouped; singletons
 *  also get a faint patch so isolated books still glow. */
function computeFamilyNebulae(g: Graph): NebulaPatch[] {
  type Acc = { xs: number[]; ys: number[]; color: string };
  const groups = new Map<string, Acc>();
  g.forEachNode((id, attrs: any) => {
    if (!isBookAnchor(id, attrs)) return;
    const fam =
      (attrs.dominant_family as string | undefined) ||
      (attrs.dominant_entity_type as string | undefined) ||
      "__none__";
    const x = Number(attrs.x);
    const y = Number(attrs.y);
    if (!Number.isFinite(x) || !Number.isFinite(y)) return;
    const acc = groups.get(fam) ?? { xs: [], ys: [], color: "" };
    acc.xs.push(x);
    acc.ys.push(y);
    // Take the first non-empty color we see — they're all the same family.
    if (!acc.color && typeof attrs.color === "string") acc.color = attrs.color;
    if (!acc.color) acc.color = colorForFamily(fam === "__none__" ? null : fam);
    groups.set(fam, acc);
  });

  const out: NebulaPatch[] = [];
  for (const [, acc] of groups) {
    if (acc.xs.length === 0) continue;
    const cx = acc.xs.reduce((a, b) => a + b, 0) / acc.xs.length;
    const cy = acc.ys.reduce((a, b) => a + b, 0) / acc.ys.length;
    let maxDist = 0;
    for (let i = 0; i < acc.xs.length; i++) {
      const dx = acc.xs[i] - cx;
      const dy = acc.ys[i] - cy;
      const d = Math.hypot(dx, dy);
      if (d > maxDist) maxDist = d;
    }
    // Singletons get a fixed small radius so they still glow.
    const radius = Math.max(maxDist * 1.4, 80);
    out.push({ cx, cy, radius, color: acc.color, count: acc.xs.length });
  }
  return out;
}
