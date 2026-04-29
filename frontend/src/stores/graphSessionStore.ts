// Mission Control session store — holds the current discovery session,
// streaming-style buffer for the latest response, and a tiny domain→color
// map that persists the same hue per domain within a corpus.

import { create } from "zustand";
import { contextGraphFromDiscoverResponse } from "../lib/contextGraph";
import type {
  DiscoverDomainSummary,
  DiscoverMode,
  GraphDiscoverResponse,
  GraphDiscoverSession,
} from "../types";

const DOMAIN_PALETTE = [
  "violet",
  "amber",
  "rose",
  "teal",
  "indigo",
  "sage",
  "sky",
  "coral",
  "olive",
  "plum",
  "slate",
  "stone",
] as const;

type DomainColor = (typeof DOMAIN_PALETTE)[number];

interface TurnRecord {
  query: string;
  mode: DiscoverMode;
  response: GraphDiscoverResponse;
  createdAt: number;
}

interface GraphNodeNavigationLink {
  label: string;
  section: "themes" | "bridges" | "gaps" | "tensions" | "trace";
  detail: string;
}

interface GraphNodeNavigation {
  entityId: string;
  entityName: string;
  conceptLabel?: string;
  links: GraphNodeNavigationLink[];
  nonce: number;
}

interface GraphSessionState {
  // Active session + corpus scope
  activeSessionId: string | null;
  activeCorpusId: string | null;

  // Turn history within the active session, most recent last.
  turns: TurnRecord[];

  // Graph canvas -> query drawer handoff. The nonce lets the panel consume
  // repeated clicks on the same entity label.
  draftQuerySeed: { text: string; nonce: number } | null;
  nodeNavigation: GraphNodeNavigation | null;

  // Canvas overlay — drives GraphView emphasis. Auto-set by pushTurn so the
  // sigma canvas re-colors by domain + highlights frontier/hub/bridge nodes
  // whenever a new discovery response arrives. Cleared on corpus/session
  // switch so stale overlays don't bleed between contexts.
  activeDiscoverGraph: GraphDiscoverResponse["graph"] | null;
  activeDiscoverContextGraph: GraphDiscoverResponse["context_graph"] | null;
  activeDiscoverDomainMap: DiscoverDomainSummary[] | null;
  activeDiscoverEntityConceptMap: GraphDiscoverResponse["entity_concept_map"] | null;

  // Currently-running request state
  loading: boolean;
  error: string | null;

  // Available sessions for the sidebar switcher
  sessions: GraphDiscoverSession[];

  // Domain→color mapping, stable per corpus
  domainColors: Record<string, Record<string, DomainColor>>;

  // Actions
  setActiveCorpus: (corpusId: string | null) => void;
  setActiveSession: (sessionId: string | null) => void;
  pushTurn: (turn: TurnRecord) => void;
  setTurns: (turns: TurnRecord[]) => void;
  resetTurns: () => void;
  clearActiveDiscoverGraph: () => void;
  seedQueryFromEntity: (entityName: string, entityId?: string) => void;
  openNodeNavigation: (entityName: string, entityId: string) => void;
  clearNodeNavigation: () => void;
  setLoading: (v: boolean) => void;
  setError: (msg: string | null) => void;
  setSessions: (sessions: GraphDiscoverSession[]) => void;
  ensureDomainColors: (corpusId: string, domains: string[]) => void;
  getDomainColor: (corpusId: string, domain: string) => DomainColor;
}

export const useGraphSessionStore = create<GraphSessionState>((set, get) => ({
  activeSessionId: null,
  activeCorpusId: null,
  turns: [],
  draftQuerySeed: null,
  nodeNavigation: null,
  activeDiscoverGraph: null,
  activeDiscoverContextGraph: null,
  activeDiscoverDomainMap: null,
  activeDiscoverEntityConceptMap: null,
  loading: false,
  error: null,
  sessions: [],
  domainColors: {},

  setActiveCorpus: (corpusId) =>
    set((state) => {
      if (state.activeCorpusId === corpusId) {
        return {
          activeCorpusId: corpusId,
          error: null,
        };
      }
      return {
        activeCorpusId: corpusId,
        activeSessionId: null,
        turns: [],
        draftQuerySeed: null,
        nodeNavigation: null,
        activeDiscoverGraph: null,
        activeDiscoverContextGraph: null,
        activeDiscoverDomainMap: null,
        activeDiscoverEntityConceptMap: null,
        error: null,
      };
    }),

  setActiveSession: (sessionId) =>
    set((state) => {
      const sameSession = sessionId === state.activeSessionId;
      return {
        activeSessionId: sessionId,
        turns: sameSession ? state.turns : [],
        draftQuerySeed: state.draftQuerySeed,
        nodeNavigation: sameSession ? state.nodeNavigation : null,
        activeDiscoverGraph: sameSession ? state.activeDiscoverGraph : null,
        activeDiscoverContextGraph: sameSession ? state.activeDiscoverContextGraph : null,
        activeDiscoverDomainMap: sameSession ? state.activeDiscoverDomainMap : null,
        activeDiscoverEntityConceptMap: sameSession
          ? state.activeDiscoverEntityConceptMap
          : null,
        error: null,
      };
    }),

  pushTurn: (turn) =>
    set((state) => ({
      turns: [...state.turns, turn],
      activeSessionId: turn.response.session_id,
      activeDiscoverGraph: turn.response.graph,
      activeDiscoverContextGraph: contextGraphFromDiscoverResponse(turn.response),
      activeDiscoverDomainMap: turn.response.domain_map_summary,
      activeDiscoverEntityConceptMap: turn.response.entity_concept_map ?? null,
      nodeNavigation: null,
    })),

  setTurns: (turns) =>
    set((state) => {
      const last = turns[turns.length - 1];
      return {
        turns,
        activeDiscoverGraph: last?.response.graph ?? state.activeDiscoverGraph,
        activeDiscoverContextGraph:
          contextGraphFromDiscoverResponse(last?.response) ?? state.activeDiscoverContextGraph,
        activeDiscoverDomainMap:
          last?.response.domain_map_summary ?? state.activeDiscoverDomainMap,
        activeDiscoverEntityConceptMap:
          last?.response.entity_concept_map ?? state.activeDiscoverEntityConceptMap,
      };
    }),

  resetTurns: () =>
    set({
      turns: [],
      activeDiscoverGraph: null,
      activeDiscoverContextGraph: null,
      activeDiscoverDomainMap: null,
      activeDiscoverEntityConceptMap: null,
      nodeNavigation: null,
    }),

  clearActiveDiscoverGraph: () =>
    set({
      activeDiscoverGraph: null,
      activeDiscoverContextGraph: null,
      activeDiscoverDomainMap: null,
      activeDiscoverEntityConceptMap: null,
      nodeNavigation: null,
    }),

  seedQueryFromEntity: (entityName, entityId) => {
    const concept = entityId
      ? get().activeDiscoverEntityConceptMap?.[entityId]
      : undefined;
    const node = entityId
      ? get().activeDiscoverGraph?.nodes.find((n) => n.id === entityId)
      : undefined;
    const peers = (concept?.top_entities || [])
      .filter((name) => name.toLowerCase() !== entityName.toLowerCase())
      .slice(0, 2);
    const conceptText = concept
      ? ` inside the ${concept.label} concept neighborhood`
      : "";
    const facetText = node?.domain_type ? ` as a ${node.domain_type}` : "";
    const peerText = peers.length > 0 ? ` (${peers.join(", ")})` : "";
    set({
      draftQuerySeed: {
        text: `Explore ${entityName}${facetText}${conceptText}${peerText} and its cross-domain bridges`,
        nonce: Date.now(),
      },
    });
  },

  openNodeNavigation: (entityName, entityId) => {
    const concept = get().activeDiscoverEntityConceptMap?.[entityId];
    const graph = get().activeDiscoverGraph;
    const contextNode = get().activeDiscoverContextGraph?.nodes.find((n) => n.id === entityId);
    const links: GraphNodeNavigationLink[] = [];
    for (const jump of contextNode?.jump_targets || []) {
      if (["themes", "bridges", "gaps", "tensions", "trace"].includes(jump.section)) {
        links.push({
          label: jump.label,
          section: jump.section as GraphNodeNavigationLink["section"],
          detail: jump.detail,
        });
      }
    }
    if (concept) {
      links.push({
        label: "theme",
        section: "themes",
        detail: concept.label,
      });
    }
    const relatedLinks = (graph?.links || []).filter(
      (link) => link.source === entityId || link.target === entityId,
    );
    if (
      relatedLinks.some((link) =>
        ["bridge", "fragile_bridge", "ghost_analogy"].includes(link.emphasis),
      )
    ) {
      links.push({
        label: "bridges",
        section: "bridges",
        detail: "bridge or analogy cards where this node appears",
      });
    }
    if (
      relatedLinks.some((link) =>
        ["weak_edge", "fragile_bridge", "gap_edge"].includes(link.emphasis),
      )
    ) {
      links.push({
        label: "gaps",
        section: "gaps",
        detail: "weak links or under-connected routes involving this node",
      });
    }
    if (links.length === 0) {
      links.push({
        label: "trace",
        section: "trace",
        detail: "working-set trace for this selected graph",
      });
    }
    set({
      nodeNavigation: {
        entityId,
        entityName,
        conceptLabel: concept?.label,
        links,
        nonce: Date.now(),
      },
    });
  },

  clearNodeNavigation: () => set({ nodeNavigation: null }),

  setLoading: (v) => set({ loading: v }),
  setError: (msg) => set({ error: msg, loading: false }),
  setSessions: (sessions) => set({ sessions }),

  ensureDomainColors: (corpusId, domains) => {
    const current = get().domainColors[corpusId] ?? {};
    const merged: Record<string, DomainColor> = { ...current };
    let i = Object.keys(merged).length;
    for (const d of domains) {
      if (!merged[d]) {
        merged[d] = DOMAIN_PALETTE[i % DOMAIN_PALETTE.length];
        i += 1;
      }
    }
    if (Object.keys(merged).length !== Object.keys(current).length) {
      set({
        domainColors: { ...get().domainColors, [corpusId]: merged },
      });
    }
  },

  getDomainColor: (corpusId, domain) => {
    const map = get().domainColors[corpusId];
    return (map && map[domain]) || "stone";
  },
}));
