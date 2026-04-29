import { useState, useEffect, useRef, useCallback } from "react";
import {
  Zap,
  Info,
  Network,
  Plus,
  Minus,
  LocateFixed,
  X,
  Loader2,
  Terminal,
  Search,
  AlertTriangle,
  Share2,
  BookText,
  Brain,
} from "lucide-react";
import ForceGraph2D from "react-force-graph-2d";
import {
  getEntityGraph,
  queryGraph,
  getDiscourseGraph,
  analyzeGraph,
} from "../../lib/api";
import { useChatStore } from "../../stores/chatStore";
import { useSettingsStore } from "../../stores/settingsStore";
import EntitySearch from "../extraction/EntitySearch";
import EntityChunkSearch from "../extraction/EntityChunkSearch";
import { DiscoveryPanel } from "./DiscoveryPanel";
import type {
  GraphBridge,
  GraphHub,
  GraphGap,
  DiscourseCluster,
  DiscourseBridge,
  DiscourseGap,
  DiscourseShape,
  DiscourseGraphResponse,
  SplitOverlayAlignment,
  GraphAnalyzeRequest,
} from "../../types";
import { Split } from "lucide-react";

type AgentMode = "knowledge" | "discourse" | "split";

interface GraphViewProps {
  onClose?: () => void;
}

// Real graph data is loaded from the API via getEntityGraph().

export function GraphView({ onClose }: GraphViewProps) {
  // UI State
  const [activeTab, setActiveTab] = useState("Agent Query");
  // Filter chips removed (user feedback): prior `activeFilter` state and
  // `filters` array dropped — they didn't actually drive any graph filtering,
  // just visual chrome.

  // Graph & Layout State
  const containerRef = useRef<HTMLDivElement>(null);
  const fgRef = useRef<any>(null);
  const [dimensions, setDimensions] = useState({ width: 0, height: 0 });
  const [graphData, setGraphData] = useState({
    nodes: [] as any[],
    links: [] as any[],
  });
  const [isHydrating, setIsHydrating] = useState(true);
  const [loadedNodes, setLoadedNodes] = useState(0);

  // Search & Highlight State
  const [searchQuery, setSearchQuery] = useState("");
  const [highlightNodes, setHighlightNodes] = useState<Set<string>>(new Set());
  const [highlightLinks, setHighlightLinks] = useState<Set<any>>(new Set());
  const [neo4jEnabled, setNeo4jEnabled] = useState(true);
  const [selectedEntityId, setSelectedEntityId] = useState<string | null>(null);

  // Phase 17 W1 — Agent Query discovery state (knowledge mode)
  const [bridges, setBridges] = useState<GraphBridge[]>([]);
  const [hubs, setHubs] = useState<GraphHub[]>([]);
  const [gaps, setGaps] = useState<GraphGap[]>([]);
  const [bridgeIds, setBridgeIds] = useState<Set<string>>(new Set());
  const [seedIds, setSeedIds] = useState<Set<string>>(new Set());
  const [isQuerying, setIsQuerying] = useState(false);
  const [queryError, setQueryError] = useState<string | null>(null);
  const [hasQueried, setHasQueried] = useState(false);

  // Phase 17 W2 — Discourse mode state (lexeme co-occurrence)
  const [agentMode, setAgentMode] = useState<AgentMode>("knowledge");
  const [lexemeIds, setLexemeIds] = useState<Set<string>>(new Set());
  const [discourseClusters, setDiscourseClusters] = useState<DiscourseCluster[]>([]);
  const [discourseBridges, setDiscourseBridges] = useState<DiscourseBridge[]>([]);
  const [discourseGaps, setDiscourseGaps] = useState<DiscourseGap[]>([]);
  const [discourseShape, setDiscourseShape] = useState<DiscourseShape | null>(null);
  const [isDiscoursing, setIsDiscoursing] = useState(false);
  const [discourseError, setDiscourseError] = useState<string | null>(null);
  const [hasDiscoursed, setHasDiscoursed] = useState(false);
  // P4 — discourse graph tuning controls
  const [discourseTopTerms, setDiscourseTopTerms] = useState(120);
  const [discourseMinCooccur, setDiscourseMinCooccur] = useState(3);
  const [discourseChunkLimit, setDiscourseChunkLimit] = useState(500);
  // Snapshot of the pre-discourse (knowledge) graph so mode-switching back
  // restores the entity canvas instead of reloading from the API.
  const knowledgeSnapshotRef = useRef<{ nodes: any[]; links: any[] } | null>(null);
  // Phase 17 W3 — keep both mode payloads available for Split + Analyze
  const knowledgeDataRef = useRef<{
    nodes: any[];
    links: any[];
    seed_ids: string[];
  }>({ nodes: [], links: [], seed_ids: [] });
  const discourseDataRef = useRef<{
    nodes: any[];
    links: any[];
    clusters: DiscourseCluster[];
    bridges: DiscourseBridge[];
    gaps: DiscourseGap[];
    shape: DiscourseShape | null;
  }>({
    nodes: [],
    links: [],
    clusters: [],
    bridges: [],
    gaps: [],
    shape: null,
  });

  // Phase 17 W3 — Split mode state (overlay + analyze + handoff)
  const [splitAlignment, setSplitAlignment] = useState<SplitOverlayAlignment | null>(null);
  const [isSplitting, setIsSplitting] = useState(false);
  const [splitError, setSplitError] = useState<string | null>(null);

  // Phase 17 W3 — LLM structural narrative per mode
  const [analysisMarkdown, setAnalysisMarkdown] = useState<string | null>(null);
  const [analysisHandoff, setAnalysisHandoff] = useState<string | null>(null);
  const [isAnalyzing, setIsAnalyzing] = useState(false);
  const [analysisError, setAnalysisError] = useState<string | null>(null);

  // Chat handoff (from store, populated on "→ Ask Chat")
  const setPendingPrompt = useChatStore((s) => s.setPendingPrompt);

  // Corpus selection comes from settingsStore (the source of truth per
  // GOTCHAS #15). chatStore.selectedCorpusIds is CorpusManager-local UI state.
  const selectedCorpusIds = useSettingsStore((s) => s.selectedCorpusIds);
  const corpusId = selectedCorpusIds[0] || null;

  // Phase 17.W1 cleanup — dropped "Table Explorer" and "Ontology" tabs
  // (declared but never rendered — dead UI entries per graphify_4 audit).
  const tabs = [
    { name: "Context Graph", icon: <Network className="w-3.5 h-3.5" /> },
    { name: "Agent Query", icon: <Zap className="w-3.5 h-3.5" /> },
    { name: "Entity Search", icon: <Search className="w-3.5 h-3.5" /> },
    { name: "Explain", icon: <Info className="w-3.5 h-3.5" /> },
  ];

  // Resize Observer
  useEffect(() => {
    if (!containerRef.current) return;
    const observer = new ResizeObserver((entries) => {
      setDimensions({
        width: entries[0].contentRect.width,
        height: entries[0].contentRect.height,
      });
    });
    observer.observe(containerRef.current);
    return () => observer.disconnect();
  }, []);

  // Load real graph data from API
  useEffect(() => {
    let cancelled = false;
    const loadGraph = async () => {
      if (!corpusId) {
        setIsHydrating(false);
        return;
      }
      setIsHydrating(true);
      try {
        const data = await getEntityGraph(corpusId);
        if (cancelled) return;
        if (!data.neo4jEnabled) {
          setNeo4jEnabled(false);
          setIsHydrating(false);
          return;
        }
        setNeo4jEnabled(true);
        setGraphData({ nodes: data.nodes, links: data.links });
        setLoadedNodes(data.nodes.length);
        setIsHydrating(false);
        setTimeout(() => {
          if (fgRef.current && data.nodes.length > 0) {
            fgRef.current.zoomToFit(400, 50);
          }
        }, 500);
      } catch {
        if (!cancelled) {
          setNeo4jEnabled(false);
          setIsHydrating(false);
        }
      }
    };
    loadGraph();
    return () => {
      cancelled = true;
    };
  }, [corpusId]);

  // -----------------------------------------------------------------------------
  // Phase 17 Wave 1 — Agent Query: backend-powered discovery
  //
  // Was: client-side string matching over cached nodes (dumb search).
  // Now: POST /api/graph/query → backend extracts entities, expands subgraph,
  //      returns bridges/hubs/gaps. Canvas reloads with the returned subgraph,
  //      DiscoveryPanel displays the structural findings.
  // -----------------------------------------------------------------------------
  const handleExecuteQuery = async (e: React.FormEvent) => {
    e.preventDefault();
    const query = searchQuery.trim();

    // Empty query → clear overlays, reheat physics, keep current graph.
    if (!query) {
      setHighlightNodes(new Set());
      setHighlightLinks(new Set());
      setBridges([]);
      setHubs([]);
      setGaps([]);
      setBridgeIds(new Set());
      setSeedIds(new Set());
      setHasQueried(false);
      setQueryError(null);
      if (fgRef.current) {
        fgRef.current.d3ReheatSimulation();
        fgRef.current.zoomToFit(800, 50);
      }
      return;
    }

    if (!corpusId) {
      setQueryError("No corpus selected");
      return;
    }

    setIsQuerying(true);
    setQueryError(null);
    try {
      const result = await queryGraph(corpusId, query, 2, 50);
      setBridges(result.bridges);
      setHubs(result.hubs);
      setGaps(result.gaps);
      setBridgeIds(new Set(result.bridges.map((b) => b.entity_id)));
      setSeedIds(new Set(result.seed_entities.map((s) => s.id)));
      setHasQueried(true);
      // Cache for Wave 3 analyzer + split overlay
      knowledgeDataRef.current = {
        nodes: result.nodes as any[],
        links: result.links as any[],
        seed_ids: result.seed_entities.map((s) => s.id),
      };
      setAnalysisMarkdown(null);
      setAnalysisHandoff(null);

      // Update the canvas with the returned subgraph. Backend nodes use
      // {id, display_name, entity_type, mention_count, is_seed} shape; map
      // to the canvas's expected {id, name, entity_type, mention_count, ...}.
      if (result.nodes.length > 0) {
        const canvasNodes = result.nodes.map((n) => ({
          id: n.id,
          name: n.display_name,
          entity_type: n.entity_type,
          mention_count: n.mention_count,
          val: Math.max(2, Math.min(n.mention_count || 1, 15)),
          color:
            n.entity_type === "person"
              ? "#3b82f6"
              : n.entity_type === "org"
                ? "#a855f7"
                : n.entity_type === "concept"
                  ? "#22c55e"
                  : "#6b7280",
        }));
        const canvasLinks = result.links.map((l) => ({
          source: l.source,
          target: l.target,
          predicate: l.predicate,
          confidence: l.confidence,
        }));
        setGraphData({ nodes: canvasNodes, links: canvasLinks });
        setLoadedNodes(canvasNodes.length);

        // Highlight bridges + seeds so the canvas visually mirrors the panel
        setHighlightNodes(
          new Set([
            ...result.bridges.map((b) => b.entity_id),
            ...result.seed_entities.map((s) => s.id),
          ]),
        );
        setHighlightLinks(new Set());

        setTimeout(() => {
          if (fgRef.current) {
            fgRef.current.d3ReheatSimulation();
            fgRef.current.zoomToFit(800, 60);
          }
        }, 100);
      } else {
        // No entities matched — keep current canvas, just clear highlights
        setHighlightNodes(new Set());
      }
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setQueryError(msg);
      setHasQueried(false);
    } finally {
      setIsQuerying(false);
    }
  };

  // Click handler — from DiscoveryPanel rows → highlight a specific entity
  const handleSelectEntity = (entityId: string) => {
    setHighlightNodes(new Set([entityId]));
    setSelectedEntityId(entityId);
    if (fgRef.current) {
      const node = graphData.nodes.find((n: any) => n.id === entityId);
      if (node && node.x !== undefined && node.y !== undefined) {
        fgRef.current.centerAt(node.x, node.y, 600);
        fgRef.current.zoom(3, 600);
      }
    }
  };

  // ---------------------------------------------------------------------------
  // Phase 17 Wave 2 — Discourse mode fetch + render
  // ---------------------------------------------------------------------------
  const fetchDiscourse = useCallback(async () => {
    if (!corpusId) {
      setDiscourseError("No corpus selected");
      return;
    }
    setIsDiscoursing(true);
    setDiscourseError(null);
    try {
      const result: DiscourseGraphResponse = await getDiscourseGraph(corpusId, {
        topTerms: discourseTopTerms,
        minCooccur: discourseMinCooccur,
        chunkLimit: discourseChunkLimit,
      });
      // Cluster-id → color. Stable HSL wheel; up to ~12 legible clusters.
      const clusterColor = (cid: number | null) => {
        if (cid == null) return "#6b7280";
        const hue = (cid * 47) % 360;
        return `hsl(${hue}, 55%, 58%)`;
      };
      const canvasNodes = result.graph.nodes.map((n) => ({
        id: n.id,
        name: n.label,
        entity_type: "lexeme",
        mention_count: n.freq,
        cluster: n.cluster,
        isLexeme: true,
        val: Math.max(2, Math.min(Math.log2(1 + n.freq) * 2, 12)),
        color: clusterColor(n.cluster),
      }));
      const canvasLinks = result.graph.links.map((l) => ({
        source: l.source,
        target: l.target,
        predicate: "co_occurs",
        confidence: Math.min(1, l.weight / 20),
        weight: l.weight,
      }));
      setGraphData({ nodes: canvasNodes, links: canvasLinks });
      setLoadedNodes(canvasNodes.length);
      setLexemeIds(new Set(canvasNodes.map((n) => n.id)));

      setDiscourseClusters(result.clusters);
      setDiscourseBridges(result.bridges);
      setDiscourseGaps(result.gaps);
      setDiscourseShape(result.shape);
      setHasDiscoursed(true);
      // Cache for Wave 3 analyzer + split overlay
      discourseDataRef.current = {
        nodes: result.graph.nodes as any[],
        links: result.graph.links as any[],
        clusters: result.clusters,
        bridges: result.bridges,
        gaps: result.gaps,
        shape: result.shape,
      };
      setAnalysisMarkdown(null);
      setAnalysisHandoff(null);

      setHighlightNodes(new Set());
      setHighlightLinks(new Set());
      setBridgeIds(new Set());
      setSeedIds(new Set());

      setTimeout(() => {
        if (fgRef.current && canvasNodes.length > 0) {
          fgRef.current.d3ReheatSimulation();
          fgRef.current.zoomToFit(800, 60);
        }
      }, 100);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      setDiscourseError(msg);
      setHasDiscoursed(false);
    } finally {
      setIsDiscoursing(false);
    }
  }, [corpusId, discourseTopTerms, discourseMinCooccur, discourseChunkLimit]);

  // Phase 17 W3 — Split mode: build merged overlay by calling analyze with
  // both snapshots. The analyze endpoint returns both the LLM narrative AND
  // the algorithmic overlay (intersection/union, crosslinks).
  const buildSplit = useCallback(async () => {
    if (!corpusId) {
      setSplitError("No corpus selected");
      return;
    }
    const k = knowledgeDataRef.current;
    const d = discourseDataRef.current;
    if (!k.nodes.length) {
      setSplitError(
        "Split requires a Knowledge query first — run an Agent Query in Knowledge mode.",
      );
      return;
    }
    if (!d.nodes.length) {
      setSplitError(
        "Split requires the Discourse graph — switch to Discourse mode and Build first.",
      );
      return;
    }

    setIsSplitting(true);
    setSplitError(null);
    setAnalysisError(null);
    try {
      const req: GraphAnalyzeRequest = {
        corpus_id: corpusId,
        mode: "split",
        query: searchQuery.trim() || null,
        knowledge: {
          nodes: k.nodes,
          links: k.links,
          seed_ids: k.seed_ids,
        },
        discourse: {
          nodes: d.nodes,
          links: d.links,
          clusters: d.clusters,
          bridges: d.bridges,
          gaps: d.gaps,
          shape: d.shape || {},
        },
      };
      const resp = await analyzeGraph(req);
      if (resp.overlay) {
        // Render the merged canvas
        const merged = resp.overlay.nodes.map((n: any) => ({
          ...n,
          // Re-derive viz fields if missing (defensive — backend preserves most)
          val: n.val ?? 5,
          color:
            n.color ??
            (n.isLexeme
              ? `hsl(${((n.cluster ?? 0) * 47) % 360}, 55%, 58%)`
              : "#3b82f6"),
          name: n.name ?? n.display_name ?? n.label ?? n.id,
        }));
        const mergedLinks = resp.overlay.links.map((l: any) => ({
          source: typeof l.source === "object" ? l.source.id : l.source,
          target: typeof l.target === "object" ? l.target.id : l.target,
          type: l.type,
          predicate: l.predicate,
          confidence: l.confidence,
          weight: l.weight,
          mode: l.mode,
        }));
        setGraphData({ nodes: merged, links: mergedLinks });
        setLoadedNodes(merged.length);
        setLexemeIds(
          new Set(merged.filter((n: any) => n.isLexeme).map((n: any) => n.id)),
        );
        setSplitAlignment(resp.overlay.alignment);
      }
      setAnalysisMarkdown(resp.markdown);
      setAnalysisHandoff(resp.handoff_prompt);

      setTimeout(() => {
        fgRef.current?.d3ReheatSimulation();
        fgRef.current?.zoomToFit(800, 60);
      }, 100);
    } catch (err) {
      setSplitError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsSplitting(false);
    }
  }, [corpusId, searchQuery]);

  // Mode-switch handler — snapshot/restore the canvas when toggling
  // between Knowledge (entity), Discourse (lexeme), and Split (merged).
  const handleSwitchMode = useCallback(
    (next: AgentMode) => {
      if (next === agentMode) return;

      if (next === "discourse") {
        knowledgeSnapshotRef.current = {
          nodes: graphData.nodes,
          links: graphData.links,
        };
        setAgentMode("discourse");
        fetchDiscourse();
      } else if (next === "split") {
        if (agentMode === "knowledge") {
          knowledgeSnapshotRef.current = {
            nodes: graphData.nodes,
            links: graphData.links,
          };
        }
        setAgentMode("split");
        buildSplit();
      } else {
        // Back to knowledge — restore snapshot or clear.
        setAgentMode("knowledge");
        setLexemeIds(new Set());
        setSplitAlignment(null);
        if (knowledgeSnapshotRef.current) {
          setGraphData(knowledgeSnapshotRef.current);
          setLoadedNodes(knowledgeSnapshotRef.current.nodes.length);
        }
        setHighlightNodes(new Set());
        setHighlightLinks(new Set());
        setTimeout(() => {
          fgRef.current?.d3ReheatSimulation();
          fgRef.current?.zoomToFit(800, 60);
        }, 100);
      }
    },
    [agentMode, graphData.nodes, graphData.links, fetchDiscourse, buildSplit],
  );

  // Run LLM structural synthesis for the current mode. Re-uses the cached
  // snapshot refs so this is a single POST with no canvas refetch.
  const runAnalyze = useCallback(async () => {
    if (!corpusId) {
      setAnalysisError("No corpus selected");
      return;
    }
    setIsAnalyzing(true);
    setAnalysisError(null);
    try {
      const k = knowledgeDataRef.current;
      const d = discourseDataRef.current;
      let req: GraphAnalyzeRequest;
      if (agentMode === "knowledge") {
        if (!k.nodes.length) {
          setAnalysisError(
            "Run an Agent Query first — no knowledge graph to analyze.",
          );
          setIsAnalyzing(false);
          return;
        }
        req = {
          corpus_id: corpusId,
          mode: "knowledge",
          query: searchQuery.trim() || null,
          knowledge: { nodes: k.nodes, links: k.links, seed_ids: k.seed_ids },
        };
      } else if (agentMode === "discourse") {
        if (!d.nodes.length) {
          setAnalysisError("Build the discourse graph first.");
          setIsAnalyzing(false);
          return;
        }
        req = {
          corpus_id: corpusId,
          mode: "discourse",
          discourse: {
            nodes: d.nodes,
            links: d.links,
            clusters: d.clusters,
            bridges: d.bridges,
            gaps: d.gaps,
            shape: d.shape || {},
          },
        };
      } else {
        // split — if we already have markdown from buildSplit, this re-runs it
        return buildSplit();
      }
      const resp = await analyzeGraph(req);
      setAnalysisMarkdown(resp.markdown);
      setAnalysisHandoff(resp.handoff_prompt);
    } catch (err) {
      setAnalysisError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsAnalyzing(false);
    }
  }, [agentMode, corpusId, searchQuery, buildSplit]);

  // "→ Ask Chat" handoff — drop the structural narrative into the chat input
  // and close the GraphView so the user lands on the chat pane.
  const handleAskChat = useCallback(() => {
    if (!analysisHandoff) return;
    setPendingPrompt(analysisHandoff);
    onClose?.();
  }, [analysisHandoff, setPendingPrompt, onClose]);

  // Click handler — from DiscoveryPanel discourse rows → highlight a lexeme
  const handleSelectLexeme = (term: string) => {
    setHighlightNodes(new Set([term]));
    if (fgRef.current) {
      const node = graphData.nodes.find((n: any) => n.id === term);
      if (node && node.x !== undefined && node.y !== undefined) {
        fgRef.current.centerAt(node.x, node.y, 600);
        fgRef.current.zoom(3, 600);
      }
    }
  };

  // -----------------------------------------------------------------------------
  // CANVAS RENDERER (Ghosting & Glowing)
  // -----------------------------------------------------------------------------
  const paintNode = useCallback(
    (node: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const isHighlighted = highlightNodes.has(node.id);
      const isFaded = highlightNodes.size > 0 && !isHighlighted;
      // Phase 17 W1 — bridges and seeds get a distinct glow even when not
      // in the active highlight set, so the user sees the structural hits
      // on the canvas in parallel with the DiscoveryPanel rows.
      const isBridge = bridgeIds.has(node.id);
      const isSeed = seedIds.has(node.id);
      // Phase 17 W2 — lexeme nodes are squares (not circles) so they're
      // visually distinguishable from entity circles in Split mode (future)
      // and self-evident in Discourse mode.
      const isLexeme = !!node.isLexeme;

      const nodeRadius = isHighlighted
        ? node.val * 1.5
        : isSeed
          ? node.val * 1.25
          : node.val;

      if (isFaded && !isBridge && !isSeed) {
        ctx.fillStyle = "rgba(255, 255, 255, 0.05)";
        ctx.shadowBlur = 0;
      } else {
        ctx.fillStyle = node.color;
        if (isHighlighted || isBridge || isSeed) {
          ctx.shadowColor = isSeed
            ? "#fbbf24"
            : isBridge
              ? "#22d3ee"
              : node.color;
          ctx.shadowBlur = isSeed ? 22 : isBridge ? 18 : 15;
        } else {
          ctx.shadowBlur = 0;
        }
      }

      // Draw body — square for lexemes, circle for entities.
      if (isLexeme) {
        ctx.fillRect(
          node.x - nodeRadius,
          node.y - nodeRadius,
          nodeRadius * 2,
          nodeRadius * 2,
        );
      } else {
        ctx.beginPath();
        ctx.arc(node.x, node.y, nodeRadius, 0, 2 * Math.PI, false);
        ctx.fill();
      }
      ctx.shadowBlur = 0; // Reset for other draws

      // Draw Text (Only if zoomed in OR node is explicitly highlighted)
      if ((globalScale > 3 && !isFaded) || isHighlighted) {
        const label = node.name;
        const fontSize = isHighlighted ? 14 / globalScale : 12 / globalScale;
        ctx.font = `${isHighlighted ? "bold " : ""}${fontSize}px "JetBrains Mono", monospace`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillStyle = isHighlighted ? "#ffffff" : "#a0a0a0";
        // Background pill for highlighted text to ensure readability
        if (isHighlighted) {
          const textWidth = ctx.measureText(label).width;
          const bgHeight = fontSize * 1.4;
          ctx.fillStyle = "rgba(0, 0, 0, 0.7)";
          ctx.fillRect(
            node.x - textWidth / 2 - 2,
            node.y + nodeRadius + 2,
            textWidth + 4,
            bgHeight,
          );
          ctx.fillStyle = "#ffffff";
        }
        ctx.fillText(
          label,
          node.x,
          node.y + nodeRadius + (isHighlighted ? 2 + fontSize / 2 : 4),
        );
      }
    },
    [highlightNodes, bridgeIds, seedIds, lexemeIds],
  );

  return (
    <div className="absolute inset-0 z-50 flex flex-col bg-[#0b0c10] text-gray-300 font-sans overflow-hidden animate-fade-in">
      {/* Top Header */}
      <header className="flex items-center justify-between px-6 py-3 border-b border-white/5 bg-[#0b0c10] shrink-0 z-20">
        <div className="flex items-center gap-4">
          <div className="w-8 h-8 rounded-full bg-[#1c4e80] flex items-center justify-center border border-[#2a6baf]">
            <Share2 className="w-4 h-4 text-white" />
          </div>
          <div>
            <h1 className="text-white text-[15px] font-semibold tracking-wide leading-tight">
              Polymath
            </h1>
            <div className="text-[10px] text-gray-500 font-mono tracking-widest uppercase mt-0.5 flex items-center gap-2">
              KNOWLEDGE GRAPH DEMO
              {isHydrating && (
                <span className="text-[#eab308] flex items-center gap-1 animate-pulse">
                  <Loader2 className="w-3 h-3 animate-spin" />
                  [HYDRATING NODE BATCH...]
                </span>
              )}
            </div>
          </div>
        </div>

        {/* Center Tabs */}
        <div className="flex items-center gap-2 bg-[#121418] p-1 rounded-lg border border-white/5">
          {tabs.map((tab) => (
            <button
              key={tab.name}
              onClick={() => setActiveTab(tab.name)}
              className={`flex items-center gap-2 px-4 py-1.5 rounded-md text-[13px] transition-all ${
                activeTab === tab.name
                  ? "bg-white/10 text-white font-medium"
                  : "text-gray-500 hover:text-gray-300 hover:bg-white/5"
              }`}
            >
              {tab.name === "Context Graph" && (
                <div className="w-1.5 h-1.5 bg-white rotate-45 opacity-70" />
              )}
              {tab.name === "Agent Query" && (
                <Zap
                  className={`w-3.5 h-3.5 ${activeTab === "Agent Query" ? "text-yellow-500" : "text-yellow-500/50"}`}
                />
              )}
              {tab.name !== "Context Graph" && tab.name !== "Agent Query" && (
                <div className="w-1.5 h-1.5 bg-gray-500 rounded-full" />
              )}
              {tab.name}
            </button>
          ))}
        </div>

        {/* Right Actions */}
        <div className="flex items-center gap-4">
          {onClose && (
            <button
              onClick={onClose}
              className="p-2 text-gray-500 hover:text-white transition-colors hover:bg-white/10 rounded-lg"
              title="Close Graph View"
            >
              <X className="w-5 h-5" />
            </button>
          )}
        </div>
      </header>

      {/* Sub-header — counts only (filter chips removed per UI feedback) */}
      <div className="flex items-center justify-end px-6 py-3 border-b border-white/5 bg-[#0b0c10] shrink-0 z-20">
        <div className="text-[12px] text-gray-400 font-mono">
          {loadedNodes} entities · {graphData.links.length} relationships
        </div>
      </div>

      {/* Main Content Area */}
      <div className="flex-1 flex overflow-hidden relative">
        {/* Left: WebGL/Canvas Physics Graph */}
        <div ref={containerRef} className="flex-1 relative bg-[#0b0c10]">
          {/* Agent Query Floating Console */}
          <div className="absolute bottom-8 left-1/2 -translate-x-1/2 z-40 w-full max-w-xl animate-slide-up flex flex-col gap-2">
            {/* Mode pills — Knowledge (default) vs Discourse */}
            <div className="flex items-center justify-center gap-1 bg-[#121418] border border-[#2a6baf]/30 p-1 rounded-lg self-center">
              <button
                onClick={() => handleSwitchMode("knowledge")}
                className={`flex items-center gap-1.5 px-3 py-1 text-[10px] font-bold tracking-widest uppercase rounded transition-colors ${
                  agentMode === "knowledge"
                    ? "bg-[#2a6baf] text-white"
                    : "text-gray-500 hover:text-gray-300 hover:bg-white/5"
                }`}
                title="Knowledge mode — entity subgraph from Neo4j, bridges/hubs/gaps"
              >
                <Brain className="w-3 h-3" />
                Knowledge
              </button>
              <button
                onClick={() => handleSwitchMode("discourse")}
                className={`flex items-center gap-1.5 px-3 py-1 text-[10px] font-bold tracking-widest uppercase rounded transition-colors ${
                  agentMode === "discourse"
                    ? "bg-[#2a6baf] text-white"
                    : "text-gray-500 hover:text-gray-300 hover:bg-white/5"
                }`}
                title="Discourse mode — lexeme co-occurrence graph from MongoDB chunks, cluster/bridge/gap/shape"
              >
                <BookText className="w-3 h-3" />
                Discourse
              </button>
              <button
                onClick={() => handleSwitchMode("split")}
                className={`flex items-center gap-1.5 px-3 py-1 text-[10px] font-bold tracking-widest uppercase rounded transition-colors ${
                  agentMode === "split"
                    ? "bg-[#2a6baf] text-white"
                    : "text-gray-500 hover:text-gray-300 hover:bg-white/5"
                }`}
                title="Split mode — entity graph overlaid with discourse graph + LLM alignment analysis"
              >
                <Split className="w-3 h-3" />
                Split
              </button>
            </div>

            {agentMode === "knowledge" ? (
              <form
                onSubmit={handleExecuteQuery}
                className="bg-[#121418] border border-[#2a6baf]/50 p-2 flex gap-3 shadow-[0_0_40px_rgba(42,107,175,0.2)] rounded-lg"
              >
                <div className="flex items-center justify-center pl-3">
                  <Search className="w-4 h-4 text-[#2a6baf]" />
                </div>
                <input
                  value={searchQuery}
                  onChange={(e) => setSearchQuery(e.target.value)}
                  placeholder="Agent Query (e.g. 'Obama and climate change')..."
                  className="flex-1 bg-transparent border-none text-white text-[14px] font-mono focus:outline-none focus:ring-0 placeholder:text-gray-600"
                />
                <button
                  type="submit"
                  disabled={isQuerying}
                  className="bg-[#2a6baf]/20 text-[#4d94ff] border border-[#2a6baf]/50 px-5 py-2 font-bold tracking-widest text-[11px] uppercase hover:bg-[#2a6baf] hover:text-white transition-colors rounded disabled:opacity-50"
                >
                  {isQuerying ? "…" : "Execute"}
                </button>
              </form>
            ) : agentMode === "discourse" ? (
              <div className="bg-[#121418] border border-[#2a6baf]/50 p-3 flex flex-col gap-2 shadow-[0_0_40px_rgba(42,107,175,0.2)] rounded-lg">
                <div className="flex items-center justify-between gap-3">
                  <div className="text-[11px] text-gray-400 font-mono tracking-wider">
                    Discourse graph for this corpus
                    {discourseShape && (
                      <span className="ml-2 text-[#4d94ff]">
                        [{discourseShape.shape}]
                      </span>
                    )}
                  </div>
                  <button
                    onClick={fetchDiscourse}
                    disabled={isDiscoursing || !corpusId}
                    className="bg-[#2a6baf]/20 text-[#4d94ff] border border-[#2a6baf]/50 px-4 py-1 font-bold tracking-widest text-[10px] uppercase hover:bg-[#2a6baf] hover:text-white transition-colors rounded disabled:opacity-50"
                  >
                    {isDiscoursing ? "…" : hasDiscoursed ? "Rebuild" : "Build"}
                  </button>
                </div>
                <div className="flex items-center gap-3 text-[9px] uppercase tracking-widest text-gray-500">
                  <label className="flex items-center gap-1">
                    <span>Top terms</span>
                    <input
                      type="number"
                      min={20}
                      max={500}
                      step={10}
                      value={discourseTopTerms}
                      onChange={(e) =>
                        setDiscourseTopTerms(
                          Math.max(20, Math.min(500, Number(e.target.value) || 120)),
                        )
                      }
                      className="w-14 bg-[#0b0c10] border border-white/10 px-1 py-0.5 text-[10px] text-white text-center font-mono rounded"
                    />
                  </label>
                  <label className="flex items-center gap-1">
                    <span>Min co-occur</span>
                    <input
                      type="number"
                      min={1}
                      max={20}
                      value={discourseMinCooccur}
                      onChange={(e) =>
                        setDiscourseMinCooccur(
                          Math.max(1, Math.min(20, Number(e.target.value) || 3)),
                        )
                      }
                      className="w-12 bg-[#0b0c10] border border-white/10 px-1 py-0.5 text-[10px] text-white text-center font-mono rounded"
                    />
                  </label>
                  <label className="flex items-center gap-1">
                    <span>Chunk cap</span>
                    <input
                      type="number"
                      min={50}
                      max={5000}
                      step={50}
                      value={discourseChunkLimit}
                      onChange={(e) =>
                        setDiscourseChunkLimit(
                          Math.max(50, Math.min(5000, Number(e.target.value) || 500)),
                        )
                      }
                      className="w-16 bg-[#0b0c10] border border-white/10 px-1 py-0.5 text-[10px] text-white text-center font-mono rounded"
                    />
                  </label>
                </div>
              </div>
            ) : (
              <div className="bg-[#121418] border border-[#2a6baf]/50 p-3 flex items-center justify-between gap-3 shadow-[0_0_40px_rgba(42,107,175,0.2)] rounded-lg">
                <div className="text-[11px] text-gray-400 font-mono tracking-wider">
                  Entity ↔ Lexeme overlay
                  {splitAlignment && (
                    <span className="ml-2 text-[#4d94ff]">
                      [{(splitAlignment.score * 100).toFixed(0)}% aligned]
                    </span>
                  )}
                </div>
                <button
                  onClick={buildSplit}
                  disabled={isSplitting || !corpusId}
                  className="bg-[#2a6baf]/20 text-[#4d94ff] border border-[#2a6baf]/50 px-4 py-1 font-bold tracking-widest text-[10px] uppercase hover:bg-[#2a6baf] hover:text-white transition-colors rounded disabled:opacity-50"
                >
                  {isSplitting ? "…" : splitAlignment ? "Rebuild" : "Build"}
                </button>
              </div>
            )}
          </div>

          {dimensions.width > 0 && dimensions.height > 0 && (
            <ForceGraph2D
              ref={fgRef}
              width={dimensions.width}
              height={dimensions.height}
              graphData={graphData}
              backgroundColor="#0b0c10"
              // Burst Physics Configuration
              cooldownTicks={100}
              onEngineStop={() =>
                console.log("[GRAPH] Physics stabilized. CPU Thread clear.")
              }
              // Visual tuning
              nodeRelSize={4}
              linkColor={(link: any) =>
                highlightLinks.has(link)
                  ? "rgba(77, 148, 255, 0.8)"
                  : "rgba(255, 255, 255, 0.03)"
              }
              linkWidth={(link: any) => (highlightLinks.has(link) ? 2 : 0.5)}
              // Particle Flow Animation for highlighted routes
              linkDirectionalParticles={(link: any) =>
                highlightLinks.has(link) ? 4 : 0
              }
              linkDirectionalParticleWidth={2}
              linkDirectionalParticleColor={() => "#4d94ff"}
              // Node Renderer
              nodeCanvasObject={paintNode}
              // Controls
              enableNodeDrag={!isHydrating}
              onNodeClick={(node) => {
                if (fgRef.current) {
                  fgRef.current.centerAt(node.x, node.y, 1000);
                  fgRef.current.zoom(4, 1000);

                  // Highlight clicked node specifically
                  setHighlightNodes(new Set([node.id]));
                  fgRef.current.d3ReheatSimulation();
                }
              }}
            />
          )}

          {/* Floating Graph Controls */}
          <div className="absolute bottom-6 right-6 flex flex-col gap-1 bg-[#121418] p-1 rounded-lg border border-white/5 shadow-xl z-30">
            <button
              onClick={() =>
                fgRef.current?.zoom(fgRef.current.zoom() * 1.5, 400)
              }
              className="p-2 text-gray-400 hover:text-white hover:bg-white/10 rounded-md transition-colors"
            >
              <Plus className="w-4 h-4" />
            </button>
            <div className="h-px bg-white/5 mx-2" />
            <button
              onClick={() =>
                fgRef.current?.zoom(fgRef.current.zoom() / 1.5, 400)
              }
              className="p-2 text-gray-400 hover:text-white hover:bg-white/10 rounded-md transition-colors"
            >
              <Minus className="w-4 h-4" />
            </button>
            <div className="h-px bg-white/5 mx-2" />
            <button
              onClick={() => {
                setHighlightNodes(new Set());
                setHighlightLinks(new Set());
                setSearchQuery("");
                fgRef.current?.zoomToFit(800, 50);
              }}
              className="p-2 text-gray-400 hover:text-white hover:bg-white/10 rounded-md transition-colors"
            >
              <LocateFixed className="w-4 h-4" />
            </button>
          </div>
        </div>

        {/* Right Sidebar: Entity Search & Inspector */}
        <div className="w-[340px] bg-[#0b0c10] border-l border-white/5 flex flex-col shrink-0 z-10 overflow-y-auto custom-scrollbar shadow-[-10px_0_30px_rgba(0,0,0,0.5)]">
          <div className="flex items-center justify-between px-6 pt-6 pb-4">
            <div className="text-[10px] font-bold text-[#4d94ff] tracking-widest uppercase flex items-center gap-2">
              <Terminal className="w-3.5 h-3.5" />
              Entity Explorer
            </div>
          </div>

          {!neo4jEnabled ? (
            <div className="px-6 py-8 text-center">
              <AlertTriangle className="w-8 h-8 text-amber-500 mx-auto mb-3" />
              <p className="text-[13px] text-gray-400 mb-2">
                Neo4j is not enabled
              </p>
              <p className="text-[11px] text-gray-600">
                Set NEO4J_ENABLED=true in .env to use the knowledge graph.
              </p>
            </div>
          ) : !corpusId ? (
            <div className="px-6 py-8 text-center">
              <p className="text-[13px] text-gray-400">
                Select a corpus to explore entities.
              </p>
            </div>
          ) : (
            <>
              <div className="px-6 pb-4">
                <EntitySearch
                  corpusId={corpusId}
                  onSelect={(entity) => {
                    setSelectedEntityId(entity.entity_id);
                    const node = graphData.nodes.find(
                      (n) => n.id === entity.entity_id,
                    );
                    if (node && fgRef.current) {
                      setHighlightNodes(new Set([node.id]));
                      fgRef.current.d3ReheatSimulation();
                      fgRef.current.centerAt(node.x, node.y, 1000);
                      fgRef.current.zoom(5, 1000);
                    }
                  }}
                />
              </div>

              {activeTab === "Entity Search" && (
                <div className="px-6 pb-6 border-t border-white/5 pt-4">
                  <div className="text-[10px] font-bold text-[#4d94ff] tracking-widest uppercase mb-3 flex items-center gap-2">
                    <Search className="w-3.5 h-3.5" />
                    Mode B — Chunk Search
                  </div>
                  <EntityChunkSearch corpusId={corpusId} />
                </div>
              )}

              <div className="px-6 flex flex-col gap-2 border-t border-white/5 pt-4 pb-4">
                <div className="text-[12px] font-mono text-gray-400 uppercase tracking-widest">
                  Graph Stats
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[11px] text-gray-500">Entities:</span>
                  <span className="text-[13px] text-white font-mono">
                    {graphData.nodes.length}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[11px] text-gray-500">Relations:</span>
                  <span className="text-[13px] text-white font-mono">
                    {graphData.links.length}
                  </span>
                </div>
                <div className="flex items-center justify-between">
                  <span className="text-[11px] text-gray-500">
                    Active State:
                  </span>
                  <span
                    className={`text-[11px] px-2 py-0.5 rounded uppercase font-bold tracking-wider ${highlightNodes.size > 0 ? "text-[#eab308] bg-[#eab308]/10" : "text-[#2dd4bf] bg-[#2dd4bf]/10"}`}
                  >
                    {highlightNodes.size > 0 ? "FOCUSED" : "IDLE"}
                  </span>
                </div>
              </div>

              {selectedEntityId &&
                (() => {
                  const node = graphData.nodes.find(
                    (n) => n.id === selectedEntityId,
                  );
                  if (!node) return null;
                  const connectedLinks = graphData.links.filter((l) => {
                    const s =
                      typeof l.source === "object" ? l.source.id : l.source;
                    const t =
                      typeof l.target === "object" ? l.target.id : l.target;
                    return s === selectedEntityId || t === selectedEntityId;
                  });
                  return (
                    <div className="px-6 pb-6 border-t border-white/5 pt-4">
                      <div className="text-[10px] text-gray-500 font-bold tracking-widest uppercase mb-3">
                        Selected Entity
                      </div>
                      <div className="bg-[#121418] border border-white/5 rounded-lg p-3 space-y-2">
                        <div className="text-[14px] text-white font-medium">
                          {node.name}
                        </div>
                        <div className="flex items-center gap-2">
                          <span
                            className="px-1.5 py-0.5 text-[10px] font-bold rounded uppercase"
                            style={{
                              backgroundColor: node.color + "22",
                              color: node.color,
                            }}
                          >
                            {node.entity_type}
                          </span>
                          <span className="text-[11px] text-gray-500">
                            ×{node.mention_count} mentions
                          </span>
                        </div>
                        <div className="text-[11px] text-gray-500">
                          {connectedLinks.length} relation
                          {connectedLinks.length !== 1 ? "s" : ""}
                        </div>
                      </div>
                    </div>
                  );
                })()}

              {/* Phase 17 W1+W2+W3 — Agent Query Discovery Panel */}
              {activeTab === "Agent Query" && (
                <div className="mt-auto">
                  {(queryError ||
                    discourseError ||
                    splitError ||
                    analysisError) && (
                    <div className="px-3 py-2 border-t border-red-500/30 bg-red-500/5 text-[10px] text-red-300 tracking-widest uppercase">
                      [ERROR]{" "}
                      {queryError ||
                        discourseError ||
                        splitError ||
                        analysisError}
                    </div>
                  )}
                  {agentMode === "knowledge" ? (
                    <DiscoveryPanel
                      mode="knowledge"
                      bridges={bridges}
                      hubs={hubs}
                      gaps={gaps}
                      onSelectEntity={handleSelectEntity}
                      isLoading={isQuerying}
                      hasQueried={hasQueried}
                      analysisMarkdown={analysisMarkdown}
                      isAnalyzing={isAnalyzing}
                      onAnalyze={runAnalyze}
                      onAskChat={handleAskChat}
                      canAnalyze={hasQueried}
                    />
                  ) : agentMode === "discourse" ? (
                    <DiscoveryPanel
                      mode="discourse"
                      clusters={discourseClusters}
                      bridges={discourseBridges}
                      gaps={discourseGaps}
                      shape={
                        discourseShape ?? {
                          shape: "EMPTY",
                          shape_description: "",
                          gini_coefficient: 0,
                          cluster_proportions: {},
                          dominant_cluster: null,
                          dominant_percentage: 0,
                          top_words_by_degree: [],
                        }
                      }
                      onSelectLexeme={handleSelectLexeme}
                      isLoading={isDiscoursing}
                      hasQueried={hasDiscoursed}
                      analysisMarkdown={analysisMarkdown}
                      isAnalyzing={isAnalyzing}
                      onAnalyze={runAnalyze}
                      onAskChat={handleAskChat}
                      canAnalyze={hasDiscoursed}
                    />
                  ) : (
                    <DiscoveryPanel
                      mode="split"
                      alignment={splitAlignment}
                      onSelectEntity={handleSelectEntity}
                      isLoading={isSplitting}
                      hasQueried={!!splitAlignment}
                      analysisMarkdown={analysisMarkdown}
                      isAnalyzing={isAnalyzing}
                      onAnalyze={runAnalyze}
                      onAskChat={handleAskChat}
                      canAnalyze={!!splitAlignment}
                    />
                  )}
                </div>
              )}
            </>
          )}
        </div>
      </div>

      {/* Bottom Status Bar */}
      <footer className="flex items-center justify-between px-6 py-2 border-t border-white/5 bg-[#0b0c10] text-[11px] font-mono text-gray-500 shrink-0 z-20">
        <div className="flex items-center gap-2">
          <div
            className={`w-1.5 h-1.5 rounded-full ${isHydrating ? "bg-[#eab308] animate-pulse" : "bg-green-500"}`}
          />
          {isHydrating ? "Processing Layout..." : "Engine Standby"}
        </div>
        <div className="flex items-center gap-2">
          <div className="w-1.5 h-1.5 bg-blue-500 rounded-full" />
          RAG Traversal <span className="text-gray-700 mx-1">|</span>{" "}
          {highlightNodes.size > 0 ? "Active Route" : "Ready"}
        </div>
      </footer>
    </div>
  );
}
