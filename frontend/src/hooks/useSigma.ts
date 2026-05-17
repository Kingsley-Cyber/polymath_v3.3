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
import type {
  SigmaNodeAttributes,
  SigmaEdgeAttributes,
} from "../lib/polymath-graph-adapter";

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
const dimColor = (hex: string, amount: number): string => {
  const rgb = hexToRgb(hex);
  const darkBg = { r: 14, g: 14, b: 22 }; // matches the canvas bg
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
  onEdgeClick?: (edgeId: string | null) => void;
  onEdgeHover?: (edgeId: string | null) => void;
  onStageClick?: () => void;
  onDoubleClickNode?: (nodeId: string) => void;
  highlightedNodeIds?: Set<string>;
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
  isLayoutRunning: boolean;
  startLayout: () => void;
  stopLayout: () => void;
  selectedNode: string | null;
  setSelectedNode: (nodeId: string | null) => void;
  selectedEdge: string | null;
  setSelectedEdge: (edgeId: string | null) => void;
}

// ── ForceAtlas2 + noverlap settings (verbatim from GitNexus) ─────────────

// Anti-overlap pass is now more aggressive — ratio 1.35 + margin 22 + 80
// iterations carves real breathing room between nodes after FA2 settles.
// Without this the no-overlap step had too little authority to undo
// FA2's central clumping.
const NOVERLAP_SETTINGS = {
  maxIterations: 80,
  ratio: 1.35,
  margin: 22,
  expansion: 1.2,
};

const getFA2Settings = (_nodeCount: number) => {
  // Constellation tuning — linLogMode OFF (was forcing the galaxy bulge),
  // scalingRatio 22 (was 12 — push nodes apart harder), gravity 0.6
  // (was 1.2 — let same-corpus clusters drift away from center instead
  // of being pulled toward it). The combined effect: same-corpus books
  // cluster by hue, distinct corpora separate into their own blobs.
  return {
    gravity: 0.6,
    scalingRatio: 22,
    slowDown: 3,
    barnesHutOptimize: _nodeCount > 200,
    barnesHutTheta: 0.6,
    strongGravityMode: false,
    outboundAttractionDistribution: true,
    linLogMode: false,
    adjustSizes: true,
    edgeWeightInfluence: 1.0,
  };
};

// Cut layout duration roughly 4-5× — settles fast enough on
// modern hardware and matches the user-perceived "load" window.
const getLayoutDuration = (nodeCount: number): number => {
  if (nodeCount > 5000) return 8000;
  if (nodeCount > 1000) return 6000;
  if (nodeCount > 500) return 5000;
  return 4000;
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
  const selectedEdgeRef = useRef<string | null>(null);
  const hoveredEdgeRef = useRef<string | null>(null);
  const highlightedRef = useRef<Set<string>>(new Set());
  const layoutTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const draggedRef = useRef<string | null>(null);
  const isDraggingRef = useRef(false);
  const optionsRef = useRef(options);
  const [isLayoutRunning, setIsLayoutRunning] = useState(false);
  const [selectedNode, setSelectedNodeState] = useState<string | null>(null);
  const [selectedEdge, setSelectedEdgeState] = useState<string | null>(null);

  // Keep options live without re-binding sigma on each render.
  useEffect(() => {
    optionsRef.current = options;
    highlightedRef.current = options.highlightedNodeIds || new Set();
    sigmaRef.current?.refresh();
  }, [options]);

  const setSelectedNode = useCallback((nodeId: string | null) => {
    selectedNodeRef.current = nodeId;
    if (nodeId) {
      selectedEdgeRef.current = null;
      setSelectedEdgeState(null);
    }
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

  const setSelectedEdge = useCallback((edgeId: string | null) => {
    selectedEdgeRef.current = edgeId;
    if (edgeId) {
      selectedNodeRef.current = null;
      setSelectedNodeState(null);
    }
    setSelectedEdgeState(edgeId);
    const sigma = sigmaRef.current;
    if (!sigma) return;
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

      defaultEdgeType: "curved",
      edgeProgramClasses: {
        curved: EdgeCurveProgram,
      },
      renderEdgeLabels: true,
      edgeLabelColor: { color: "#e4e4ed" },
      edgeLabelSize: 10,
      edgeLabelFont:
        "Inter, JetBrains Mono, ui-sans-serif, system-ui, sans-serif",
      edgeLabelWeight: "500",

      // Custom hover renderer — dark pill with colored border + glow ring
      // around the node. Verbatim signature from GitNexus.
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
        // Dark pill background.
        context.fillStyle = "#0d0d14";
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
        // Border matching node color.
        context.strokeStyle = data.color || "#6366f1";
        context.lineWidth = 2;
        context.stroke();
        // Label text — light slate.
        context.fillStyle = "#f5f5f7";
        context.textAlign = "center";
        context.textBaseline = "middle";
        context.fillText(label, x, y);
        // Subtle glow ring around the node.
        context.beginPath();
        context.arc(data.x, data.y, nodeSize + 4, 0, Math.PI * 2);
        context.strokeStyle = data.color || "#6366f1";
        context.lineWidth = 2;
        context.globalAlpha = 0.5;
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
        const selectedEdge = selectedEdgeRef.current;
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

        if (selectedEdge && !sel) {
          const g = graphRef.current;
          if (g && g.hasEdge(selectedEdge)) {
            const [source, target] = g.extremities(selectedEdge);
            const isEndpoint = node === source || node === target;
            if (isEndpoint) {
              res.color = brightenColor(data.color, 1.2);
              res.size = (data.size || 8) * 1.2;
              res.zIndex = 2;
              res.forceLabel = true;
            } else {
              res.color = dimColor(data.color, 0.22);
              res.size = (data.size || 8) * 0.65;
              res.zIndex = 0;
            }
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
              // Was *1.8 — produced 25px blobs from 14px Domain nodes
              // on click. 1.25 keeps the highlight readable without
              // overwhelming neighbors.
              res.size = (data.size || 8) * 1.25;
              res.zIndex = 2;
              res.highlighted = true;
              res.forceLabel = true;
            } else if (isNeighbor) {
              res.color = data.color;
              // Was *1.3 — neighbors swelled almost as much as the
              // selection. 1.1 nudges them just enough to register
              // visually without competing for attention.
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
          res.color = brightenColor(data.color, 1.25);
          res.size = (data.size || 8) * 1.25;
          res.zIndex = 2;
          res.forceLabel = true;
        } else if (data.isHub) {
          res.color = brightenColor(data.color, 1.1);
          res.zIndex = 1;
        }
        return res;
      },

      edgeReducer: (edge: string, data: any) => {
        const res = { ...data };
        const sel = selectedNodeRef.current;
        const selectedEdge = selectedEdgeRef.current;
        const hoveredEdge = hoveredEdgeRef.current;
        const highlighted = highlightedRef.current;
        const hasHighlights = highlighted.size > 0;
        const shouldShowLabel = edge === selectedEdge || edge === hoveredEdge;
        if (!shouldShowLabel) {
          res.label = undefined;
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
              res.label = data.label;
            } else {
              res.color = dimColor(data.color, 0.1);
              res.size = 0.3;
              res.zIndex = 0;
              res.label = undefined;
            }
          }
          return res;
        }

        if (selectedEdge) {
          if (edge === selectedEdge) {
            res.color = brightenColor(data.color, 1.55);
            res.size = Math.max(3, (data.size || 1) * 3.5);
            res.zIndex = 3;
            res.label = data.label;
          } else {
            res.color = dimColor(data.color, 0.12);
            res.size = 0.25;
            res.zIndex = 0;
            res.label = undefined;
          }
          return res;
        }

        if (hoveredEdge && edge === hoveredEdge) {
          res.color = brightenColor(data.color, 1.35);
          res.size = Math.max(2, (data.size || 1) * 2);
          res.zIndex = 2;
          res.label = data.label;
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
      const isLargeGraph = nodeCount > 800;
      const isHugeGraph = nodeCount > 3000;
      // Camera tier: closer in = lower ratio. Defaults Sigma uses ~1.0.
      const zoomedFar = ratio >= 4;
      const zoomedMid = ratio >= 1.5 && ratio < 4;
      // Take the STRICTER of zoom-tier and node-count rules.
      const renderLabels = !zoomedFar && !isLargeGraph;
      const labelDensity =
        zoomedMid || isLargeGraph ? (isHugeGraph ? 0.02 : 0.05) : 0.1;
      const labelThreshold =
        zoomedMid || isLargeGraph ? (isHugeGraph ? 14 : 11) : 8;
      try {
        s.setSetting("renderLabels", renderLabels);
        s.setSetting("labelDensity", labelDensity);
        s.setSetting("labelRenderedSizeThreshold", labelThreshold);
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
      setSelectedEdge(null);
      setSelectedNode(node);
      optionsRef.current.onNodeClick?.(node);
    });
    sigma.on("clickEdge", ({ edge, event }: any) => {
      event?.preventSigmaDefault?.();
      setSelectedEdge(edge);
      optionsRef.current.onEdgeClick?.(edge);
    });
    sigma.on("clickStage", () => {
      setSelectedNode(null);
      setSelectedEdge(null);
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
    sigma.on("enterEdge", ({ edge }: any) => {
      hoveredEdgeRef.current = edge;
      optionsRef.current.onEdgeHover?.(edge);
      sigmaRef.current?.refresh();
      if (containerRef.current) containerRef.current.style.cursor = "pointer";
    });
    sigma.on("leaveEdge", () => {
      hoveredEdgeRef.current = null;
      optionsRef.current.onEdgeHover?.(null);
      sigmaRef.current?.refresh();
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
  }, [setSelectedEdge, setSelectedNode]);

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
      const customSettings = getFA2Settings(nodeCount);
      const settings = { ...inferredSettings, ...customSettings };

      const layout = new FA2Layout(graph, { settings });
      layoutRef.current = layout;
      layout.start();
      setIsLayoutRunning(true);

      const duration = durationOverrideMs ?? getLayoutDuration(nodeCount);
      layoutTimeoutRef.current = setTimeout(() => {
        if (layoutRef.current) {
          layoutRef.current.stop();
          layoutRef.current = null;
          try {
            noverlap.assign(graph, NOVERLAP_SETTINGS);
          } catch {
            /* ignore */
          }
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
      // ── LOD: gate labels by node count (PRD §F performance rule) ──
      // Below 800 → labels on, normal density. Above → keep only
      // forceLabel:true nodes (Book anchors, seeds) visible, drop density
      // and raise the size threshold so the canvas stays readable at scale.
      const nodeCount = newGraph.order;
      const isLargeGraph = nodeCount > 800;
      const isHugeGraph = nodeCount > 3000;
      try {
        sigma.setSetting("renderLabels", !isLargeGraph);
        sigma.setSetting(
          "labelDensity",
          isHugeGraph ? 0.02 : isLargeGraph ? 0.05 : 0.1,
        );
        sigma.setSetting(
          "labelRenderedSizeThreshold",
          isHugeGraph ? 14 : isLargeGraph ? 11 : 8,
        );
      } catch {
        /* setSetting may throw if the renderer is mid-frame — ignore */
      }
      sigma.setGraph(newGraph);
      setSelectedNode(null);
      setSelectedEdge(null);
      runLayout(newGraph);
      sigma.getCamera().animatedReset({ duration: 500 });
    },
    [runLayout, setSelectedEdge, setSelectedNode],
  );

  const zoomIn = useCallback(() => {
    sigmaRef.current?.getCamera().animatedZoom({ duration: 200 });
  }, []);

  const zoomOut = useCallback(() => {
    sigmaRef.current?.getCamera().animatedUnzoom({ duration: 200 });
  }, []);

  const resetZoom = useCallback(() => {
    sigmaRef.current?.getCamera().animatedReset({ duration: 300 });
    setSelectedNode(null);
    setSelectedEdge(null);
  }, [setSelectedEdge, setSelectedNode]);

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
          noverlap.assign(graph, NOVERLAP_SETTINGS);
        } catch {
          /* ignore */
        }
        sigmaRef.current?.refresh();
      }
      setIsLayoutRunning(false);
    }
  }, []);

  return {
    containerRef,
    sigmaRef,
    setGraph,
    zoomIn,
    zoomOut,
    resetZoom,
    isLayoutRunning,
    startLayout,
    stopLayout,
    selectedNode,
    setSelectedNode,
    selectedEdge,
    setSelectedEdge,
  };
};
