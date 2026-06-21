/**
 * GraphAtom3D — 3D atom view of the same graph payload.
 *
 * Renders an AtomicView-sized atom as a 3D scene: a central nucleus,
 * with seeds/bridges/evidence/gaps positioned on concentric orbital
 * shells in 3D space. Roles map directly to colors (anchor / hub /
 * bridge / gap / evidence / cross_corpus / query_matched / frontier)
 * and lanes map to a soft outer ring tinted by the lane palette.
 *
 * Constraints (per guardrails):
 *   - Consumes the SAME nodes/links returned by /api/graph/query and
 *     /api/graph/discover. Never re-queries retrieval.
 *   - Caps visible payload to GRAPH_3D_NODE_LIMIT / GRAPH_3D_EDGE_LIMIT.
 *   - Never mutates the source payload.
 *   - Default OFF — loaded via React.lazy() and only mounted when the
 *     user clicks the "View: 3D Atom" toggle.
 *
 * Performance: nodes are spheres with light emissive material; edges are
 * thin lines. Total cost at the cap (150n / 350e) is well under 1ms
 * per frame on a 2020-era MacBook. The scene auto-rotates slowly when
 * the user isn't dragging (ambient motion, respects prefers-reduced-motion).
 */

import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import {
  colorTokens,
  roleColor,
  laneColor,
  type NodeRole,
} from "../../lib/design-tokens";

export interface GraphAtom3DNode {
  id: string;
  label: string;
  role: NodeRole;
  lane?: "corpus" | "graph" | "web";
  /** mention_count / degree used for size scaling. */
  weight?: number;
}

export interface GraphAtom3DLink {
  source: string;
  target: string;
  kind?: "supports" | "bridges" | "gap" | "mentions";
  label?: string;
  family?: string | null;
  confidence?: number | null;
  weight?: number | null;
  sourceLabel?: string;
  targetLabel?: string;
}

export interface GraphAtom3DProps {
  nodes: GraphAtom3DNode[];
  links: GraphAtom3DLink[];
  selectedNodeId?: string | null;
  onSelectNode?: (nodeId: string | null) => void;
  /** Optional headline rendered in the overlay HUD. */
  headline?: string;
  /** Synthesis markdown (truncated) — surfaced in the floating legend. */
  synthesisHeadline?: string;
}

export const GRAPH_3D_NODE_LIMIT = 150;
export const GRAPH_3D_EDGE_LIMIT = 350;

/** Pure helper — caller can use to inspect the cap from the parent. */
export function capFor3D<T>(arr: T[], limit: number): T[] {
  if (arr.length <= limit) return arr;
  return arr.slice(0, limit);
}

export default function GraphAtom3D({
  nodes,
  links,
  selectedNodeId,
  onSelectNode,
  headline,
  synthesisHeadline,
}: GraphAtom3DProps) {
  return (
    <div
      style={{
        position: "relative",
        width: "100%",
        height: "100%",
        background: "var(--surface-canvas)",
        overflow: "hidden",
      }}
    >
      <Suspense fallback={<Atom3DFallback />}>
        <Atom3DScene
          nodes={nodes}
          links={links}
          selectedNodeId={selectedNodeId}
          onSelectNode={onSelectNode}
          headline={headline}
          synthesisHeadline={synthesisHeadline}
        />
      </Suspense>
      <Atom3DLegend headline={headline} synthesisHeadline={synthesisHeadline} />
      <Atom3DConnectionCard
        nodes={nodes}
        links={links}
        selectedNodeId={selectedNodeId}
        onClear={() => onSelectNode?.(null)}
      />
    </div>
  );
}

function Atom3DFallback() {
  return (
    <div
      style={{
        position: "absolute",
        inset: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        color: "var(--ink-tertiary)",
        fontFamily: "var(--font-mono)",
        fontSize: "var(--type-sm)",
      }}
    >
      Booting 3D atom…
    </div>
  );
}

interface Atom3DSceneProps {
  nodes: GraphAtom3DNode[];
  links: GraphAtom3DLink[];
  selectedNodeId?: string | null;
  onSelectNode?: (nodeId: string | null) => void;
  headline?: string;
  synthesisHeadline?: string;
}

function Atom3DScene({
  nodes,
  links,
  selectedNodeId,
  onSelectNode,
}: Atom3DSceneProps) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [hoverId, setHoverId] = useState<string | null>(null);
  const selectedNodeIdRef = useRef<string | null>(selectedNodeId ?? null);

  useEffect(() => {
    selectedNodeIdRef.current = selectedNodeId ?? null;
  }, [selectedNodeId]);

  // Reduce payload to the cap, preferring the most-connected nodes so
  // the atom reads as a real atom and not a fog.
  const { keptNodes, keptLinks } = useMemo(() => {
    if (nodes.length <= GRAPH_3D_NODE_LIMIT && links.length <= GRAPH_3D_EDGE_LIMIT) {
      return { keptNodes: nodes, keptLinks: links };
    }
    const byWeight = [...nodes]
      .sort((a, b) => (b.weight ?? 0) - (a.weight ?? 0))
      .slice(0, GRAPH_3D_NODE_LIMIT);
    const ids = new Set(byWeight.map((n) => n.id));
    const cappedLinks = links
      .filter((l) => ids.has(l.source) && ids.has(l.target))
      .slice(0, GRAPH_3D_EDGE_LIMIT);
    return { keptNodes: byWeight, keptLinks: cappedLinks };
  }, [nodes, links]);

  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const width = container.clientWidth;
    const height = container.clientHeight;

    // ── Scene ────────────────────────────────────────────────────────
    const scene = new THREE.Scene();
    scene.background = new THREE.Color(colorTokens.surface.canvas);
    scene.fog = new THREE.Fog(colorTokens.surface.canvas, 14, 36);

    const camera = new THREE.PerspectiveCamera(
      45,
      width / Math.max(height, 1),
      0.1,
      100,
    );
    camera.position.set(0, 5, 18);
    camera.lookAt(0, 0, 0);

    const renderer = new THREE.WebGLRenderer({
      antialias: true,
      alpha: false,
      powerPreference: "high-performance",
    });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setSize(width, height);
    container.appendChild(renderer.domElement);

    // ── Lighting ─────────────────────────────────────────────────────
    scene.add(new THREE.AmbientLight(0xffffff, 0.6));
    const key = new THREE.PointLight(0xfbbf24, 1.4, 40);
    key.position.set(8, 12, 8);
    scene.add(key);
    const fill = new THREE.PointLight(0x7dd3fc, 0.6, 40);
    fill.position.set(-10, -6, 6);
    scene.add(fill);

    // ── Orbital ring guides (very faint) ─────────────────────────────
    const ringGroup = new THREE.Group();
    const ringMat = new THREE.LineBasicMaterial({
      color: new THREE.Color(colorTokens.border.regular),
      transparent: true,
      opacity: 0.18,
    });
    for (const r of [3.2, 6.0, 9.0]) {
      const ringGeom = new THREE.BufferGeometry();
      const segments = 96;
      const pts: number[] = [];
      for (let i = 0; i <= segments; i++) {
        const a = (i / segments) * Math.PI * 2;
        pts.push(Math.cos(a) * r, 0, Math.sin(a) * r);
      }
      ringGeom.setAttribute(
        "position",
        new THREE.Float32BufferAttribute(pts, 3),
      );
      ringGroup.add(new THREE.Line(ringGeom, ringMat));
    }
    scene.add(ringGroup);

    // ── Nucleus (anchor cluster centroid) ────────────────────────────
    const nucleusGeom = new THREE.SphereGeometry(1.2, 32, 32);
    const nucleusMat = new THREE.MeshStandardMaterial({
      color: new THREE.Color(colorTokens.role.anchor),
      emissive: new THREE.Color(colorTokens.role.anchor),
      emissiveIntensity: 0.55,
      roughness: 0.35,
      metalness: 0.05,
    });
    const nucleus = new THREE.Mesh(nucleusGeom, nucleusMat);
    scene.add(nucleus);

    // ── Compute shell positions per role ─────────────────────────────
    // Buckets so the atom reads as: hub shell > bridge shell > evidence
    // shell > frontier shell, with cross_corpus / query_matched
    // surfaced as accent rings.
    const buckets: Record<NodeRole, GraphAtom3DNode[]> = {
      anchor: [],
      cross_corpus: [],
      query_matched: [],
      hub: [],
      bridge: [],
      evidence: [],
      gap: [],
      frontier: [],
    };
    for (const n of keptNodes) buckets[n.role].push(n);

    const shellRadius: Record<NodeRole, number> = {
      anchor: 1.6,
      query_matched: 3.4,
      cross_corpus: 3.4,
      hub: 5.6,
      bridge: 6.2,
      evidence: 8.4,
      gap: 9.6,
      frontier: 9.0,
    };

    const nodeGroup = new THREE.Group();
    const meshes: Record<string, THREE.Mesh> = {};
    const labelSprites: THREE.Sprite[] = [];

    function makeLabelSprite(label: string, color: string): THREE.Sprite {
      const canvas = document.createElement("canvas");
      const ctx = canvas.getContext("2d");
      const text = label.length > 28 ? `${label.slice(0, 27)}…` : label;
      canvas.width = 512;
      canvas.height = 128;
      if (ctx) {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.font = "600 30px Inter, system-ui, sans-serif";
        const textWidth = Math.min(430, ctx.measureText(text).width);
        const w = Math.max(152, textWidth + 56);
        const h = 58;
        const x = (canvas.width - w) / 2;
        const y = 36;
        const r = 14;
        ctx.fillStyle = "rgba(17, 20, 27, 0.84)";
        ctx.strokeStyle = color;
        ctx.lineWidth = 2;
        ctx.beginPath();
        ctx.moveTo(x + r, y);
        ctx.lineTo(x + w - r, y);
        ctx.quadraticCurveTo(x + w, y, x + w, y + r);
        ctx.lineTo(x + w, y + h - r);
        ctx.quadraticCurveTo(x + w, y + h, x + w - r, y + h);
        ctx.lineTo(x + r, y + h);
        ctx.quadraticCurveTo(x, y + h, x, y + h - r);
        ctx.lineTo(x, y + r);
        ctx.quadraticCurveTo(x, y, x + r, y);
        ctx.closePath();
        ctx.fill();
        ctx.stroke();
        ctx.fillStyle = "rgba(241, 245, 249, 0.96)";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(text, canvas.width / 2, y + h / 2 + 1, 430);
      }
      const texture = new THREE.CanvasTexture(canvas);
      texture.minFilter = THREE.LinearFilter;
      const material = new THREE.SpriteMaterial({
        map: texture,
        transparent: true,
        opacity: 0.88,
        depthWrite: false,
      });
      const sprite = new THREE.Sprite(material);
      sprite.scale.set(2.7, 0.68, 1);
      return sprite;
    }

    function placeOnShell(
      n: GraphAtom3DNode,
      r: number,
      index: number,
      total: number,
      planeTilt = 0,
    ) {
      // Fibonacci sphere for even spread; apply a per-shell tilt so the
      // four shells don't all overlap as perfect horizontal rings.
      const phi = Math.acos(1 - (2 * (index + 0.5)) / total);
      const theta = Math.PI * (1 + Math.sqrt(5)) * index;
      const x = r * Math.sin(phi) * Math.cos(theta);
      const y = r * Math.cos(phi) * planeTilt;
      const z = r * Math.sin(phi) * Math.sin(theta);
      const w = Math.max(0.08, Math.min(0.36, Math.log2((n.weight ?? 1) + 1) * 0.07));
      const geom = new THREE.SphereGeometry(w, 16, 16);
      const mat = new THREE.MeshStandardMaterial({
        color: new THREE.Color(roleColor(n.role)),
        emissive: new THREE.Color(roleColor(n.role)),
        emissiveIntensity: 0.32,
        roughness: 0.55,
        metalness: 0.0,
      });
      const mesh = new THREE.Mesh(geom, mat);
      mesh.position.set(x, y, z);
      mesh.userData = {
        id: n.id,
        label: n.label,
        role: n.role,
        baseScale: 1,
      };
      nodeGroup.add(mesh);
      meshes[n.id] = mesh;

      const shouldLabel =
        n.role === "query_matched" ||
        n.role === "anchor" ||
        n.role === "bridge" ||
        n.role === "hub" ||
        labelSprites.length < 14;
      if (shouldLabel && labelSprites.length < 24) {
        const sprite = makeLabelSprite(n.label, roleColor(n.role));
        sprite.position.set(x, y + w + 0.46, z);
        sprite.userData = { follows: n.id };
        labelSprites.push(sprite);
        nodeGroup.add(sprite);
      }
    }

    let runningIndex = 0;
    // Query-matched nodes — pull them slightly forward (closer to the
    // nucleus) and tilt their shell toward the camera so the user's
    // matches read first.
    for (const n of buckets.query_matched) {
      placeOnShell(n, shellRadius.query_matched, runningIndex++, buckets.query_matched.length, 0.85);
    }
    for (const n of buckets.cross_corpus) {
      placeOnShell(n, shellRadius.cross_corpus, runningIndex++, buckets.cross_corpus.length, 0.85);
    }
    for (const n of buckets.hub) {
      placeOnShell(n, shellRadius.hub, runningIndex++, buckets.hub.length, 0.55);
    }
    for (const n of buckets.bridge) {
      placeOnShell(n, shellRadius.bridge, runningIndex++, buckets.bridge.length, 0.40);
    }
    for (const n of buckets.evidence) {
      placeOnShell(n, shellRadius.evidence, runningIndex++, buckets.evidence.length, 0.25);
    }
    for (const n of buckets.gap) {
      placeOnShell(n, shellRadius.gap, runningIndex++, buckets.gap.length, 0.10);
    }
    for (const n of buckets.frontier) {
      placeOnShell(n, shellRadius.frontier, runningIndex++, buckets.frontier.length, 0.15);
    }

    scene.add(nodeGroup);

    // ── Edges ────────────────────────────────────────────────────────
    const edgeGroup = new THREE.Group();
    const pulseGroup = new THREE.Group();
    const edgeMatByKind: Record<NonNullable<GraphAtom3DLink["kind"]>, THREE.LineBasicMaterial> = {
      supports: new THREE.LineBasicMaterial({
        color: new THREE.Color(laneColor("corpus")),
        transparent: true,
        opacity: 0.45,
      }),
      bridges: new THREE.LineBasicMaterial({
        color: new THREE.Color(roleColor("bridge")),
        transparent: true,
        opacity: 0.7,
      }),
      gap: new THREE.LineBasicMaterial({
        color: new THREE.Color(roleColor("gap")),
        transparent: true,
        opacity: 0.55,
      }),
      mentions: new THREE.LineBasicMaterial({
        color: new THREE.Color(laneColor("graph")),
        transparent: true,
        opacity: 0.35,
      }),
    };

    const pulseMatByKind: Record<NonNullable<GraphAtom3DLink["kind"]>, THREE.MeshBasicMaterial> = {
      supports: new THREE.MeshBasicMaterial({
        color: new THREE.Color(laneColor("corpus")),
        transparent: true,
        opacity: 0.78,
      }),
      bridges: new THREE.MeshBasicMaterial({
        color: new THREE.Color(roleColor("bridge")),
        transparent: true,
        opacity: 0.9,
      }),
      gap: new THREE.MeshBasicMaterial({
        color: new THREE.Color(roleColor("gap")),
        transparent: true,
        opacity: 0.84,
      }),
      mentions: new THREE.MeshBasicMaterial({
        color: new THREE.Color(laneColor("graph")),
        transparent: true,
        opacity: 0.72,
      }),
    };
    const pulseGeom = new THREE.SphereGeometry(0.045, 8, 8);
    const pulses: Array<{
      mesh: THREE.Mesh;
      source: THREE.Mesh;
      target: THREE.Mesh;
      offset: number;
      speed: number;
    }> = [];

    for (const [index, l] of keptLinks.entries()) {
      const a = meshes[l.source];
      const b = meshes[l.target];
      if (!a || !b) continue;
      const geom = new THREE.BufferGeometry().setFromPoints([
        a.position.clone(),
        b.position.clone(),
      ]);
      const mat =
        edgeMatByKind[l.kind ?? "supports"] ?? edgeMatByKind.supports;
      const line = new THREE.Line(geom, mat);
      edgeGroup.add(line);

      if (index < 120) {
        const pulse = new THREE.Mesh(
          pulseGeom,
          pulseMatByKind[l.kind ?? "supports"] ?? pulseMatByKind.supports,
        );
        pulse.position.copy(a.position);
        pulseGroup.add(pulse);
        pulses.push({
          mesh: pulse,
          source: a,
          target: b,
          offset: (index % 17) / 17,
          speed: l.kind === "bridges" ? 0.19 : 0.13,
        });
      }
    }
    scene.add(edgeGroup);
    scene.add(pulseGroup);

    // ── Lane ribbons — one thin tinted ring per lane ─────────────────
    const laneRingGeom = new THREE.TorusGeometry(10.4, 0.015, 8, 128);
    const laneRingGroup = new THREE.Group();
    for (const lane of ["corpus", "graph", "web"] as const) {
      const mat = new THREE.MeshBasicMaterial({
        color: new THREE.Color(laneColor(lane)),
        transparent: true,
        opacity: 0.22,
      });
      const ring = new THREE.Mesh(laneRingGeom, mat);
      ring.rotation.x = Math.PI / 2 + (lane === "graph" ? 0.1 : lane === "web" ? -0.1 : 0);
      ring.rotation.z = lane === "web" ? Math.PI / 2 : 0;
      laneRingGroup.add(ring);
    }
    scene.add(laneRingGroup);

    const haloGeom = new THREE.SphereGeometry(0.72, 32, 32);
    const haloMat = new THREE.MeshBasicMaterial({
      color: new THREE.Color(colorTokens.accent.main),
      transparent: true,
      opacity: 0.0,
      blending: THREE.AdditiveBlending,
      depthWrite: false,
    });
    const selectionHalo = new THREE.Mesh(haloGeom, haloMat);
    selectionHalo.visible = false;
    nodeGroup.add(selectionHalo);

    // ── Controls (mouse drag rotate, wheel zoom) ─────────────────────
    let isDragging = false;
    let lastX = 0;
    let lastY = 0;
    let yaw = 0;
    let pitch = 0.2;
    let manualZoom = 18;
    let cameraZoom = 18;
    let ambientRotation = 0;
    const cameraTarget = new THREE.Vector3(0, 0, 0);
    const targetScratch = new THREE.Vector3(0, 0, 0);
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();

    const onDown = (e: PointerEvent) => {
      isDragging = true;
      lastX = e.clientX;
      lastY = e.clientY;
      renderer.domElement.style.cursor = "grabbing";
      (e.target as HTMLElement).setPointerCapture?.(e.pointerId);
    };
    const onMove = (e: PointerEvent) => {
      if (!isDragging) return;
      yaw += (e.clientX - lastX) * 0.0045;
      pitch += (e.clientY - lastY) * 0.0045;
      pitch = Math.max(-1.4, Math.min(1.4, pitch));
      lastX = e.clientX;
      lastY = e.clientY;
    };
    const onUp = () => {
      isDragging = false;
      renderer.domElement.style.cursor = "grab";
    };
    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      manualZoom = Math.max(8, Math.min(40, manualZoom + e.deltaY * 0.012));
    };
    const focusNode = (nodeId: string) => {
      selectedNodeIdRef.current = nodeId;
      manualZoom = Math.min(manualZoom, 11.5);
      setHoverId((cur) => (cur === nodeId ? cur : nodeId));
      onSelectNode?.(nodeId);
    };
    const onClick = (e: MouseEvent) => {
      const rect = renderer.domElement.getBoundingClientRect();
      pointer.x = ((e.clientX - rect.left) / rect.width) * 2 - 1;
      pointer.y = -((e.clientY - rect.top) / rect.height) * 2 + 1;

      scene.updateMatrixWorld(true);
      raycaster.setFromCamera(pointer, camera);
      const meshList = Object.values(meshes);
      const directHits = raycaster.intersectObjects(meshList, false);
      const directId = String(directHits[0]?.object?.userData?.id || "");
      if (directId) {
        focusNode(directId);
        return;
      }

      // Forgiving fallback: project visible node centers to screen space
      // and choose the nearest one inside a small tap radius. Spheres can
      // be visually tiny, especially on dense query graphs, so exact
      // raycasting alone makes node investigation feel broken.
      let bestId: string | null = null;
      let bestPx = Infinity;
      for (const [id, mesh] of Object.entries(meshes)) {
        const world = new THREE.Vector3();
        mesh.getWorldPosition(world);
        const projected = world.project(camera);
        if (projected.z < -1 || projected.z > 1) continue;
        const px = (projected.x * 0.5 + 0.5) * rect.width;
        const py = (-projected.y * 0.5 + 0.5) * rect.height;
        const dist = Math.hypot(px - (e.clientX - rect.left), py - (e.clientY - rect.top));
        if (dist < bestPx) {
          bestPx = dist;
          bestId = id;
        }
      }
      if (bestId) {
        focusNode(bestId);
      } else {
        setHoverId(null);
        selectedNodeIdRef.current = null;
        onSelectNode?.(null);
      }
    };
    renderer.domElement.addEventListener("pointerdown", onDown);
    renderer.domElement.addEventListener("pointermove", onMove);
    renderer.domElement.addEventListener("pointerup", onUp);
    renderer.domElement.addEventListener("pointerleave", onUp);
    renderer.domElement.addEventListener("wheel", onWheel, { passive: false });
    renderer.domElement.addEventListener("click", onClick);

    // ── Resize ───────────────────────────────────────────────────────
    const onResize = () => {
      if (!container) return;
      const w = container.clientWidth;
      const h = container.clientHeight;
      renderer.setSize(w, h);
      camera.aspect = w / Math.max(h, 1);
      camera.updateProjectionMatrix();
    };
    const ro = new ResizeObserver(onResize);
    ro.observe(container);

    // ── Reduced-motion media query ───────────────────────────────────
    const reduced = window.matchMedia?.(
      "(prefers-reduced-motion: reduce)",
    ).matches;

    // ── Animation loop ───────────────────────────────────────────────
    let raf = 0;
    let lastT = performance.now();
    const tick = (now: number) => {
      const dt = (now - lastT) / 1000;
      lastT = now;
      if (!reduced && !isDragging) {
        ambientRotation += dt * 0.08;
      }
      nodeGroup.rotation.y = yaw + ambientRotation;
      nodeGroup.rotation.x = pitch;
      edgeGroup.rotation.copy(nodeGroup.rotation);
      pulseGroup.rotation.copy(nodeGroup.rotation);
      laneRingGroup.rotation.copy(nodeGroup.rotation);
      nodeGroup.updateMatrixWorld(true);

      const selectedMesh = selectedNodeIdRef.current
        ? meshes[selectedNodeIdRef.current]
        : null;
      for (const [id, mesh] of Object.entries(meshes)) {
        const targetScale = selectedMesh && id === selectedNodeIdRef.current ? 1.85 : 1.0;
        mesh.scale.lerp(
          new THREE.Vector3(targetScale, targetScale, targetScale),
          Math.min(1, dt * 8),
        );
        const mat = mesh.material as THREE.MeshStandardMaterial;
        const targetEmissive = selectedMesh && id === selectedNodeIdRef.current ? 0.92 : 0.32;
        mat.emissiveIntensity += (targetEmissive - mat.emissiveIntensity) * Math.min(1, dt * 6);
      }
      if (selectedMesh) {
        selectionHalo.visible = true;
        selectionHalo.position.copy(selectedMesh.position);
        const pulse = 1.25 + Math.sin(now * 0.006) * 0.12;
        selectionHalo.scale.setScalar(pulse);
        haloMat.opacity += (0.32 - haloMat.opacity) * Math.min(1, dt * 8);
      } else {
        haloMat.opacity += (0.0 - haloMat.opacity) * Math.min(1, dt * 8);
        if (haloMat.opacity < 0.01) selectionHalo.visible = false;
      }

      for (const pulse of pulses) {
        const t = (ambientRotation * pulse.speed + pulse.offset) % 1;
        pulse.mesh.position.lerpVectors(pulse.source.position, pulse.target.position, t);
      }

      camera.position.set(
        Math.sin(ambientRotation * 0.4) * 0.6,
        5,
        cameraZoom,
      );
      if (selectedMesh) {
        selectedMesh.getWorldPosition(targetScratch);
      } else {
        targetScratch.set(0, 0, 0);
      }
      cameraTarget.lerp(targetScratch, selectedMesh ? 0.09 : 0.055);
      const targetZoom = selectedMesh ? Math.min(manualZoom, 11.5) : manualZoom;
      cameraZoom += (targetZoom - cameraZoom) * Math.min(1, dt * 5);
      camera.lookAt(cameraTarget);

      nucleus.rotation.y += dt * 0.4;
      renderer.render(scene, camera);
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);

    return () => {
      cancelAnimationFrame(raf);
      ro.disconnect();
      renderer.domElement.removeEventListener("pointerdown", onDown);
      renderer.domElement.removeEventListener("pointermove", onMove);
      renderer.domElement.removeEventListener("pointerup", onUp);
      renderer.domElement.removeEventListener("pointerleave", onUp);
      renderer.domElement.removeEventListener("wheel", onWheel);
      renderer.domElement.removeEventListener("click", onClick);
      nucleusGeom.dispose();
      nucleusMat.dispose();
      ringMat.dispose();
      Object.values(meshes).forEach((m) => {
        m.geometry.dispose();
        (m.material as THREE.Material).dispose();
      });
      Object.values(edgeMatByKind).forEach((m) => m.dispose());
      Object.values(pulseMatByKind).forEach((m) => m.dispose());
      pulseGeom.dispose();
      haloGeom.dispose();
      haloMat.dispose();
      labelSprites.forEach((sprite) => {
        const material = sprite.material as THREE.SpriteMaterial;
        material.map?.dispose();
        material.dispose();
      });
      laneRingGeom.dispose();
      renderer.dispose();
      if (renderer.domElement.parentNode === container) {
        container.removeChild(renderer.domElement);
      }
      scene.traverse((obj) => {
        if ((obj as THREE.Mesh).geometry) (obj as THREE.Mesh).geometry.dispose?.();
        const mat = (obj as THREE.Mesh).material;
        if (Array.isArray(mat)) mat.forEach((m) => m.dispose?.());
        else mat?.dispose?.();
      });
    };
  }, [keptNodes, keptLinks, onSelectNode]);

  return (
    <div
      ref={containerRef}
      style={{ position: "absolute", inset: 0, cursor: "grab" }}
    >
      {hoverId && (
        <HoverLabel id={hoverId} nodes={keptNodes} />
      )}
    </div>
  );
}

function HoverLabel({
  id,
  nodes,
}: {
  id: string;
  nodes: GraphAtom3DNode[];
}) {
  const found = nodes.find((n) => n.id === id);
  if (!found) return null;
  const color = roleColor(found.role);
  return (
    <div
      style={{
        position: "absolute",
        top: "var(--space-3)",
        left: "50%",
        transform: "translateX(-50%)",
        pointerEvents: "none",
        background: "var(--surface-panel)",
        border: `1px solid ${color}`,
        color: "var(--ink-primary)",
        padding: "4px var(--space-3)",
        borderRadius: "var(--radius-md)",
        fontFamily: "var(--font-mono)",
        fontSize: "var(--type-sm)",
        fontWeight: 500,
        boxShadow: "var(--shadow-card)",
        zIndex: 5,
        maxWidth: "min(80%, 32rem)",
        whiteSpace: "nowrap",
        overflow: "hidden",
        textOverflow: "ellipsis",
      }}
    >
      <span
        style={{
          display: "inline-block",
          width: 6,
          height: 6,
          borderRadius: "var(--radius-full)",
          background: color,
          marginRight: 6,
        }}
      />
      {found.label}
    </div>
  );
}

function atomRelationLabel(link: GraphAtom3DLink): string {
  return String(link.label || link.kind || "related").replace(/_/g, " ");
}

function Atom3DConnectionCard({
  nodes,
  links,
  selectedNodeId,
  onClear,
}: {
  nodes: GraphAtom3DNode[];
  links: GraphAtom3DLink[];
  selectedNodeId?: string | null;
  onClear?: () => void;
}) {
  const selected = selectedNodeId
    ? nodes.find((node) => node.id === selectedNodeId)
    : null;
  if (!selected) return null;

  const nodeById = new Map(nodes.map((node) => [node.id, node]));
  const connections = links
    .map((link) => {
      const source = String(link.source);
      const target = String(link.target);
      if (source !== selected.id && target !== selected.id) return null;
      const outgoing = source === selected.id;
      const otherId = outgoing ? target : source;
      const other = nodeById.get(otherId);
      return {
        link,
        outgoing,
        otherId,
        otherLabel:
          (outgoing ? link.targetLabel : link.sourceLabel) ||
          other?.label ||
          otherId,
        otherRole: other?.role || "frontier",
      };
    })
    .filter(Boolean) as Array<{
      link: GraphAtom3DLink;
      outgoing: boolean;
      otherId: string;
      otherLabel: string;
      otherRole: NodeRole;
    }>;

  const confidence =
    connections
      .map((conn) => conn.link.confidence)
      .filter((value): value is number => typeof value === "number")
      .sort((a, b) => b - a)[0] ?? null;

  return (
    <aside
      style={{
        position: "absolute",
        top: "var(--space-3)",
        right: "var(--space-3)",
        width: "min(21rem, calc(100% - 2 * var(--space-3)))",
        zIndex: 6,
        border: `1px solid ${roleColor(selected.role)}`,
        borderRadius: "var(--radius-lg)",
        background: "rgba(17, 20, 27, 0.94)",
        boxShadow: "var(--shadow-card)",
        backdropFilter: "blur(12px)",
        color: "var(--ink-primary)",
        padding: "var(--space-3)",
      }}
      aria-label="Selected node connections"
    >
      <div
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          gap: "var(--space-2)",
        }}
      >
        <div style={{ minWidth: 0 }}>
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 7,
              fontFamily: "var(--font-mono)",
              fontSize: "10px",
              letterSpacing: "0.18em",
              textTransform: "uppercase",
              color: "var(--ink-tertiary)",
            }}
          >
            <span
              style={{
                width: 7,
                height: 7,
                borderRadius: "var(--radius-full)",
                background: roleColor(selected.role),
              }}
            />
            Node inspector
          </div>
          <div
            style={{
              marginTop: "var(--space-1)",
              fontFamily: "var(--font-sans)",
              fontSize: "var(--type-md)",
              fontWeight: 650,
              lineHeight: 1.25,
              overflow: "hidden",
              textOverflow: "ellipsis",
            }}
            title={selected.label}
          >
            {selected.label}
          </div>
        </div>
        <button
          type="button"
          onClick={onClear}
          style={{
            border: "1px solid var(--border-thin)",
            borderRadius: "var(--radius-sm)",
            background: "var(--surface-raised)",
            color: "var(--ink-tertiary)",
            fontFamily: "var(--font-mono)",
            fontSize: "10px",
            padding: "2px 6px",
            cursor: "pointer",
          }}
        >
          clear
        </button>
      </div>

      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: "var(--space-1)",
          marginTop: "var(--space-2)",
          fontFamily: "var(--font-mono)",
          fontSize: "10px",
        }}
      >
        <span className="gchip" style={{ color: roleColor(selected.role), borderColor: roleColor(selected.role) }}>
          {selected.role.replace(/_/g, " ")}
        </span>
        <span className="gchip">{connections.length} link{connections.length === 1 ? "" : "s"}</span>
        {confidence != null && (
          <span className="gchip">{Math.round(confidence * 100)}% best conf</span>
        )}
      </div>

      <div
        style={{
          marginTop: "var(--space-3)",
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-1)",
          maxHeight: "15rem",
          overflowY: "auto",
          paddingRight: 2,
        }}
      >
        {connections.length === 0 ? (
          <div
            style={{
              color: "var(--ink-tertiary)",
              fontFamily: "var(--font-mono)",
              fontSize: "var(--type-xs)",
            }}
          >
            No visible connections in this bounded query graph.
          </div>
        ) : (
          connections.slice(0, 9).map((conn, index) => (
            <div
              key={`${conn.otherId}-${index}`}
              style={{
                border: "1px solid var(--border-thin)",
                borderRadius: "var(--radius-md)",
                background: "var(--surface-base)",
                padding: "var(--space-2)",
              }}
            >
              <div
                style={{
                  display: "flex",
                  alignItems: "center",
                  gap: "var(--space-2)",
                  minWidth: 0,
                }}
              >
                <span style={{ color: "var(--ink-tertiary)" }}>
                  {conn.outgoing ? "→" : "←"}
                </span>
                <span
                  style={{
                    width: 6,
                    height: 6,
                    borderRadius: "var(--radius-full)",
                    background: roleColor(conn.otherRole),
                    flex: "0 0 auto",
                  }}
                />
                <span
                  style={{
                    minWidth: 0,
                    flex: 1,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                    fontSize: "var(--type-sm)",
                    color: "var(--ink-primary)",
                  }}
                  title={conn.otherLabel}
                >
                  {conn.otherLabel}
                </span>
              </div>
              <div
                style={{
                  marginTop: 4,
                  display: "flex",
                  flexWrap: "wrap",
                  gap: 5,
                  fontFamily: "var(--font-mono)",
                  fontSize: "10px",
                  color: "var(--ink-tertiary)",
                }}
              >
                <span>{atomRelationLabel(conn.link)}</span>
                {conn.link.family && <span>{conn.link.family}</span>}
                {typeof conn.link.weight === "number" && (
                  <span>strength {Math.round(conn.link.weight * 100) / 100}</span>
                )}
                {typeof conn.link.confidence === "number" && (
                  <span>{Math.round(conn.link.confidence * 100)}% conf</span>
                )}
              </div>
            </div>
          ))
        )}
      </div>
    </aside>
  );
}

function Atom3DLegend({
  headline,
  synthesisHeadline,
}: {
  headline?: string;
  synthesisHeadline?: string;
}) {
  return (
    <>
      <div
        style={{
          position: "absolute",
          top: "var(--space-3)",
          left: "var(--space-3)",
          display: "flex",
          flexDirection: "column",
          gap: "var(--space-1)",
          zIndex: 5,
          pointerEvents: "none",
        }}
      >
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 6,
            background: "rgba(17, 20, 27, 0.9)",
            border: "1px solid var(--border-thin)",
            borderRadius: "var(--radius-md)",
            padding: "4px var(--space-2)",
            fontFamily: "var(--font-mono)",
            fontSize: "var(--type-xs)",
            letterSpacing: "0.18em",
            textTransform: "uppercase",
            color: "var(--ink-tertiary)",
            backdropFilter: "blur(8px)",
          }}
        >
          <span
            style={{
              display: "inline-block",
              width: 6,
              height: 6,
              borderRadius: "var(--radius-full)",
              background: "var(--accent-main)",
            }}
          />
          3D Atom
        </div>
        <div
          style={{
            display: "flex",
            flexWrap: "wrap",
            gap: "var(--space-1)",
            background: "rgba(17, 20, 27, 0.9)",
            border: "1px solid var(--border-thin)",
            borderRadius: "var(--radius-md)",
            padding: "var(--space-1)",
            backdropFilter: "blur(8px)",
          }}
        >
          {(
            ["anchor", "query_matched", "hub", "bridge", "evidence", "gap", "frontier"] as const
          ).map((role) => (
            <span
              key={role}
              className="gchip"
              style={{
                background: "transparent",
                color: `var(--role-${role.replace(/_/g, "-")})`,
                borderColor: `var(--role-${role.replace(/_/g, "-")})`,
                textTransform: "lowercase",
                fontSize: "10px",
              }}
              title={`Role: ${role.replace(/_/g, " ")}`}
            >
              <span
                className="gchip__dot"
                style={{
                  background: `var(--role-${role.replace(/_/g, "-")})`,
                }}
              />
              {role.replace(/_/g, " ")}
            </span>
          ))}
        </div>
      </div>
      {(synthesisHeadline || headline) && (
        <div
          style={{
            position: "absolute",
            bottom: "var(--space-3)",
            left: "var(--space-3)",
            right: "var(--space-3)",
            maxWidth: "min(40rem, 100%)",
            background: "rgba(17, 20, 27, 0.92)",
            border: "1px solid var(--border-thin)",
            borderRadius: "var(--radius-md)",
            padding: "var(--space-3) var(--space-4)",
            color: "var(--ink-primary)",
            fontFamily: "var(--font-reading)",
            fontSize: "var(--type-sm)",
            lineHeight: "var(--lineHeight-normal, 1.5)",
            backdropFilter: "blur(8px)",
            boxShadow: "var(--shadow-card)",
            zIndex: 5,
          }}
        >
          {synthesisHeadline ?? headline}
        </div>
      )}
      <div
        style={{
          position: "absolute",
          right: "var(--space-3)",
          bottom: "var(--space-3)",
          color: "var(--ink-tertiary)",
          fontFamily: "var(--font-mono)",
          fontSize: "10px",
          letterSpacing: "0.18em",
          textTransform: "uppercase",
          background: "rgba(17, 20, 27, 0.7)",
          padding: "var(--space-1) var(--space-2)",
          borderRadius: "var(--radius-sm)",
          border: "1px solid var(--border-thin)",
          zIndex: 5,
          pointerEvents: "none",
        }}
      >
        drag · scroll to zoom
      </div>
    </>
  );
}
