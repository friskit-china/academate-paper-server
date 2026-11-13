"use client";

import ELK from "elkjs/lib/elk.bundled.js";
import {
  Background,
  ConnectionMode,
  Controls,
  MarkerType,
  MiniMap,
  Panel,
  Position,
  ReactFlow,
  type ReactFlowInstance,
  type Edge,
  type Connection,
  type EdgeMouseHandler,
  type Node,
  type NodeMouseHandler,
  type OnMoveEnd,
  type Viewport,
  useEdgesState,
  useNodesState,
} from "@xyflow/react";
import {
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import {
  createPaperMapConcept,
  createPaperMapEdge,
  createPaperMapFollowup,
  deletePaperMapEdge,
  deletePaperMapNode,
  generatePaperMapAnswer,
  generatePaperVisualMap,
  getPaperMapQuestionLibrary,
  getPaperVisualMap,
  updatePaperMapEdge,
  type PaperMapNode,
  type PaperMapEdge,
  type PaperMapQuestion,
  type PaperMapQuestionLibraryItem,
  type PaperVisualMapEnvelope,
} from "../../api";
import { jumpToPaperBlock } from "./paper-block-navigation";

type StoredMapUi = {
  collapsed?: boolean;
  height?: number;
  viewport?: Viewport;
  positions?: Record<string, { x: number; y: number }>;
  collapsedNodeIds?: string[];
};

const elk = new ELK();
const NODE_WIDTH = 286;
const NODE_HEIGHT = 116;
const CONCEPT_TYPES = [
  "CLAIM",
  "METHOD",
  "EVIDENCE",
  "DATASET",
  "METRIC",
  "ARTIFACT",
  "LIMITATION",
  "FUTURE",
  "SETTING",
  "IDEA",
  "MOTIVATION",
  "RESULT",
  "ASSUMPTION",
  "CONTRIBUTION",
] as const;

type ChildComposer = {
  parentId: string;
  mode: "CHOICE" | "QUESTION" | "CONCEPT";
};

type WheelGestureMode = "pan" | "zoom";

type WheelGestureMemory = {
  mode: WheelGestureMode;
  timestamp: number;
};

type WheelEventWithLegacyDelta = WheelEvent & {
  wheelDeltaY?: number;
};

const WHEEL_GESTURE_GAP_MS = 180;

function paperMapWheelMode(
  event: WheelEventWithLegacyDelta,
  previous: WheelGestureMemory | null,
): WheelGestureMode {
  const timestamp = event.timeStamp;
  const continuingGesture = previous && timestamp - previous.timestamp <= WHEEL_GESTURE_GAP_MS;
  const horizontal = Math.abs(event.deltaX) > 0.01;
  const legacyDelta = Math.abs(event.wheelDeltaY ?? 0);
  const notchedMouseWheel = !horizontal
    && legacyDelta >= 120
    && Math.abs(legacyDelta % 120) < 0.01;

  if (event.deltaMode !== 0 || notchedMouseWheel) return "zoom";
  if (horizontal || (legacyDelta > 0 && !notchedMouseWheel) || !Number.isInteger(event.deltaY)) {
    return "pan";
  }
  if (continuingGesture) return previous.mode;

  // Browsers do not expose the input-device type. Small pixel deltas are the safest
  // fallback for touchpad motion; larger ambiguous vertical steps remain mouse zoom.
  return Math.abs(event.deltaY) < 40 ? "pan" : "zoom";
}

function storageKey(paperId: string): string {
  return `research-paper-workspace.paper-map.${paperId}`;
}

function readStoredUi(paperId: string): StoredMapUi {
  if (typeof window === "undefined") return {};
  try {
    return JSON.parse(window.localStorage.getItem(storageKey(paperId)) ?? "{}") as StoredMapUi;
  } catch {
    return {};
  }
}

function saveStoredUi(paperId: string, patch: Partial<StoredMapUi>): void {
  const current = readStoredUi(paperId);
  window.localStorage.setItem(storageKey(paperId), JSON.stringify({ ...current, ...patch }));
}

function nodeClass(node: PaperMapNode): string {
  const subtype = node.concept_type ? ` paper-map-concept-${node.concept_type.toLowerCase()}` : "";
  return `paper-map-flow-node paper-map-node-${node.node_type.toLowerCase()}${subtype}`;
}

function treeChildCounts(mapEdges: PaperMapEdge[]): Map<string, number> {
  const counts = new Map<string, number>();
  for (const edge of mapEdges) {
    if (edge.is_tree) counts.set(edge.source_node_id, (counts.get(edge.source_node_id) ?? 0) + 1);
  }
  return counts;
}

function hiddenDescendantIds(
  collapsedNodeIds: Set<string>,
  mapEdges: PaperMapEdge[],
): Set<string> {
  const children = new Map<string, string[]>();
  for (const edge of mapEdges) {
    if (!edge.is_tree) continue;
    children.set(edge.source_node_id, [
      ...(children.get(edge.source_node_id) ?? []),
      edge.target_node_id,
    ]);
  }
  const hidden = new Set<string>();
  for (const collapsedNodeId of collapsedNodeIds) {
    const pending = [...(children.get(collapsedNodeId) ?? [])];
    while (pending.length) {
      const descendantId = pending.pop();
      if (!descendantId || hidden.has(descendantId)) continue;
      hidden.add(descendantId);
      pending.push(...(children.get(descendantId) ?? []));
    }
  }
  return hidden;
}

function graphStructureSignature(mapNodes: PaperMapNode[], mapEdges: PaperMapEdge[]): string {
  const nodeIds = mapNodes.map((node) => node.id).sort();
  const edgeIds = mapEdges
    .map((edge) => `${edge.id}:${edge.source_node_id}:${edge.target_node_id}`)
    .sort();
  return `${nodeIds.join("|")}::${edgeIds.join("|")}`;
}

function labelForNode(
  node: PaperMapNode,
  childCount = 0,
  isCollapsed = false,
  onToggle?: () => void,
  onAdd?: () => void,
): ReactNode {
  return (
    <div className="paper-map-node-label">
      <span>{node.node_type === "CONCEPT" && node.concept_type
        ? `CONCEPT: ${node.concept_type.replaceAll("_", " ")}`
        : node.node_type.replaceAll("_", " ")}</span>
      <strong>{node.label}</strong>
      {node.status ? <small>{node.status.replaceAll("_", " ")}</small> : null}
      {childCount ? (
        <button
          aria-label={`${isCollapsed ? "展开" : "折叠"}子节点：${node.label}`}
          className="nodrag nopan paper-map-node-collapse"
          onClick={(event) => {
            event.stopPropagation();
            onToggle?.();
          }}
          title={isCollapsed ? "展开子节点" : "折叠子节点"}
          type="button"
        >
          <span>{isCollapsed ? "+" : "−"}</span>
          <small>{childCount}</small>
        </button>
      ) : null}
      <button
        aria-label={`新增子节点：${node.label}`}
        className="nodrag nopan paper-map-node-add"
        onClick={(event) => {
          event.stopPropagation();
          onAdd?.();
        }}
        title="新增子节点"
        type="button"
      >＋</button>
    </div>
  );
}

function flowEdgeFor(edge: PaperMapEdge, hiddenNodeIds: Set<string>): Edge {
  const editableLabel = edge.label === null
    ? (edge.is_tree ? undefined : edge.edge_type.replaceAll("_", " "))
    : edge.label || undefined;
  return {
    id: edge.id,
    source: edge.source_node_id,
    target: edge.target_node_id,
    label: editableLabel,
    animated: !edge.is_tree,
    className: edge.is_tree ? "paper-map-tree-edge" : "paper-map-cross-edge",
    data: { storedLabel: edge.label, edgeType: edge.edge_type },
    hidden: hiddenNodeIds.has(edge.source_node_id) || hiddenNodeIds.has(edge.target_node_id),
    interactionWidth: 24,
    labelBgBorderRadius: 6,
    labelBgPadding: [6, 4],
    markerEnd: { type: MarkerType.ArrowClosed },
  };
}

async function layoutMap(
  mapNodes: PaperMapNode[],
  mapEdges: Edge[],
  storedPositions: StoredMapUi["positions"],
  collapsedNodeIds: Set<string> = new Set(),
  hiddenNodeIds: Set<string> = new Set(),
  childCounts: Map<string, number> = new Map(),
  onToggle?: (nodeId: string) => void,
  onAdd?: (nodeId: string) => void,
): Promise<Node[]> {
  const graph = await elk.layout({
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.spacing.nodeNode": "42",
      "elk.layered.spacing.nodeNodeBetweenLayers": "72",
      "elk.layered.nodePlacement.strategy": "NETWORK_SIMPLEX",
    },
    children: mapNodes.filter((node) => !hiddenNodeIds.has(node.id)).map((node) => ({
      id: node.id,
      width: NODE_WIDTH,
      height: NODE_HEIGHT,
    })),
    edges: mapEdges.filter(
      (edge) => !hiddenNodeIds.has(edge.source) && !hiddenNodeIds.has(edge.target),
    ).map((edge) => ({
      id: edge.id,
      sources: [edge.source],
      targets: [edge.target],
    })),
  });
  const positions = new Map(
    (graph.children ?? []).map((child) => [child.id, { x: child.x ?? 0, y: child.y ?? 0 }]),
  );
  return mapNodes.map((node) => ({
    id: node.id,
    className: nodeClass(node),
    data: {
      label: labelForNode(
        node,
        childCounts.get(node.id) ?? 0,
        collapsedNodeIds.has(node.id),
        () => onToggle?.(node.id),
        () => onAdd?.(node.id),
      ),
      mapNode: node,
    },
    hidden: hiddenNodeIds.has(node.id),
    position: storedPositions?.[node.id] ?? positions.get(node.id) ?? { x: 0, y: 0 },
    sourcePosition: Position.Right,
    style: { width: NODE_WIDTH, minHeight: NODE_HEIGHT },
    targetPosition: Position.Left,
  }));
}

function questionStateLabel(question: PaperMapQuestion): string {
  if (question.status === "QUEUED") return "已排队";
  if (question.status === "RUNNING") return "生成中…";
  if (question.status === "FAILED") return "生成失败";
  if (question.status === "SUCCEEDED") return "已回答";
  return "尚未回答";
}

function nodeCenter(node: Node): { x: number; y: number } {
  const width = node.measured?.width ?? node.width ?? NODE_WIDTH;
  const height = node.measured?.height ?? node.height ?? NODE_HEIGHT;
  return {
    x: node.position.x + width / 2,
    y: node.position.y + height / 2,
  };
}

function selectedFocusPoint(
  focusNodes: Node[],
  focusEdges: Edge[],
  selectedNodeId: string | null,
  selectedEdgeId: string | null,
): { x: number; y: number } | null {
  if (selectedNodeId) {
    const node = focusNodes.find((candidate) => candidate.id === selectedNodeId && !candidate.hidden);
    if (node) return nodeCenter(node);
  }
  if (!selectedEdgeId) return null;
  const edge = focusEdges.find((candidate) => candidate.id === selectedEdgeId && !candidate.hidden);
  if (!edge) return null;
  const source = focusNodes.find((node) => node.id === edge.source && !node.hidden);
  const target = focusNodes.find((node) => node.id === edge.target && !node.hidden);
  if (!source || !target) return null;
  const sourceCenter = nodeCenter(source);
  const targetCenter = nodeCenter(target);
  return {
    x: (sourceCenter.x + targetCenter.x) / 2,
    y: (sourceCenter.y + targetCenter.y) / 2,
  };
}

export function PaperVisualMap({ paperId }: { paperId: string }) {
  const initialUi = useMemo(() => readStoredUi(paperId), [paperId]);
  const [envelope, setEnvelope] = useState<PaperVisualMapEnvelope | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [collapsed, setCollapsed] = useState(initialUi.collapsed ?? false);
  const [height, setHeight] = useState(initialUi.height ?? 560);
  const [collapsedNodeIds, setCollapsedNodeIds] = useState(
    () => new Set(initialUi.collapsedNodeIds ?? []),
  );
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [childComposer, setChildComposer] = useState<ChildComposer | null>(null);
  const [questionDraft, setQuestionDraft] = useState("");
  const [questionLibrary, setQuestionLibrary] = useState<PaperMapQuestionLibraryItem[] | null>(null);
  const [questionLibraryLoading, setQuestionLibraryLoading] = useState(false);
  const [questionLibraryError, setQuestionLibraryError] = useState<string | null>(null);
  const [selectedLibraryQuestionKey, setSelectedLibraryQuestionKey] = useState("");
  const [conceptType, setConceptType] = useState<(typeof CONCEPT_TYPES)[number]>("CLAIM");
  const [conceptTitle, setConceptTitle] = useState("");
  const [conceptBody, setConceptBody] = useState("");
  const [edgeEditor, setEdgeEditor] = useState<{ edgeId: string; label: string } | null>(null);
  const [deleteNodeId, setDeleteNodeId] = useState<string | null>(null);
  const [confirmRegeneration, setConfirmRegeneration] = useState(false);
  const [requestedGeneration, setRequestedGeneration] = useState<number | null>(null);
  const flowRef = useRef<ReactFlowInstance<Node, Edge> | null>(null);
  const flowContainerRef = useRef<HTMLDivElement | null>(null);
  const wheelGestureRef = useRef<WheelGestureMemory | null>(null);
  const pendingTrackpadDeltaRef = useRef({ x: 0, y: 0 });
  const trackpadPanFrameRef = useRef<number | null>(null);
  const viewportSaveTimerRef = useRef<number | null>(null);
  const [nodes, setNodes, onNodesChange] = useNodesState<Node>([]);
  const [edges, setEdges, onEdgesChange] = useEdgesState<Edge>([]);
  const resizeStart = useRef<{ y: number; height: number } | null>(null);
  const paperMapRef = useRef(envelope?.paper_map ?? null);
  const collapsedNodeIdsRef = useRef(collapsedNodeIds);
  const toggleNodeCollapseRef = useRef<(nodeId: string) => void>(() => undefined);
  const openChildComposerRef = useRef<(nodeId: string) => void>(() => undefined);
  const nodesRef = useRef(nodes);
  const edgesRef = useRef(edges);
  const collapseLayoutVersionRef = useRef(0);
  const mutationLayoutAnchorRef = useRef<string | null>(null);
  const graphStructureSignatureRef = useRef<string | null>(null);
  const selectedNodeIdRef = useRef(selectedNodeId);
  const selectedEdgeIdRef = useRef(selectedEdgeId);

  paperMapRef.current = envelope?.paper_map ?? null;
  collapsedNodeIdsRef.current = collapsedNodeIds;
  nodesRef.current = nodes;
  edgesRef.current = edges;
  selectedNodeIdRef.current = selectedNodeId;
  selectedEdgeIdRef.current = selectedEdgeId;

  const focusSelectedItem = useCallback((focusNodes: Node[], focusEdges: Edge[]): boolean => {
    const instance = flowRef.current;
    const point = selectedFocusPoint(
      focusNodes,
      focusEdges,
      selectedNodeIdRef.current,
      selectedEdgeIdRef.current,
    );
    if (!instance || !point) return false;
    const zoom = instance.getViewport().zoom;
    window.setTimeout(() => {
      void flowRef.current?.setCenter(point.x, point.y, {
        zoom,
        duration: 320,
        ease: (progress) => 1 - (1 - progress) ** 3,
        interpolate: "smooth",
      });
    }, 0);
    return true;
  }, []);

  const load = useCallback(async () => {
    try {
      const next = await getPaperVisualMap(paperId);
      setEnvelope(next);
      setError(null);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法加载论文理解图。");
    }
  }, [paperId]);

  useEffect(() => {
    void load();
  }, [load]);

  useEffect(() => {
    const container = flowContainerRef.current;
    if (!container) return;

    const saveTrackpadViewport = (viewport: Viewport) => {
      if (viewportSaveTimerRef.current !== null) {
        window.clearTimeout(viewportSaveTimerRef.current);
      }
      viewportSaveTimerRef.current = window.setTimeout(() => {
        saveStoredUi(paperId, { viewport });
        viewportSaveTimerRef.current = null;
      }, 160);
    };

    const applyTrackpadPan = () => {
      trackpadPanFrameRef.current = null;
      const instance = flowRef.current;
      const delta = pendingTrackpadDeltaRef.current;
      pendingTrackpadDeltaRef.current = { x: 0, y: 0 };
      if (!instance || (!delta.x && !delta.y)) return;
      const current = instance.getViewport();
      const next = {
        x: current.x - delta.x,
        y: current.y - delta.y,
        zoom: current.zoom,
      };
      void instance.setViewport(next);
      saveTrackpadViewport(next);
    };

    const handleAdaptiveWheel = (rawEvent: WheelEvent) => {
      const event = rawEvent as WheelEventWithLegacyDelta;
      const target = event.target;
      if (!(target instanceof Element) || !target.closest(".react-flow")) return;

      // Chromium/WebKit report a trackpad pinch as ctrl+wheel. React Flow already
      // handles that path correctly, so it must never be mistaken for two-finger pan.
      if (event.ctrlKey) return;

      const mode = paperMapWheelMode(event, wheelGestureRef.current);
      wheelGestureRef.current = { mode, timestamp: event.timeStamp };
      if (mode !== "pan") return;

      event.preventDefault();
      event.stopPropagation();
      pendingTrackpadDeltaRef.current.x += event.deltaX;
      pendingTrackpadDeltaRef.current.y += event.deltaY;
      if (trackpadPanFrameRef.current === null) {
        trackpadPanFrameRef.current = window.requestAnimationFrame(applyTrackpadPan);
      }
    };

    container.addEventListener("wheel", handleAdaptiveWheel, { capture: true, passive: false });
    return () => {
      container.removeEventListener("wheel", handleAdaptiveWheel, { capture: true });
      if (trackpadPanFrameRef.current !== null) {
        window.cancelAnimationFrame(trackpadPanFrameRef.current);
        trackpadPanFrameRef.current = null;
      }
      if (viewportSaveTimerRef.current !== null) {
        window.clearTimeout(viewportSaveTimerRef.current);
        viewportSaveTimerRef.current = null;
      }
      pendingTrackpadDeltaRef.current = { x: 0, y: 0 };
      wheelGestureRef.current = null;
    };
  }, [collapsed, paperId]);

  const active = Boolean(envelope?.paper_map && (
    ["QUEUED", "RUNNING"].includes(envelope.paper_map.status)
    || envelope.paper_map.questions.some((question) =>
      ["QUEUED", "RUNNING"].includes(question.status))
  ));
  const mapQuestions = envelope?.paper_map?.questions ?? [];
  const completedQuestionCount = mapQuestions.filter(
    (question) => question.status === "SUCCEEDED",
  ).length;
  const failedQuestionCount = mapQuestions.filter(
    (question) => question.status === "FAILED",
  ).length;
  const waitingQuestionCount = mapQuestions.filter(
    (question) => question.status === "NOT_REQUESTED",
  ).length;
  const progressTone = failedQuestionCount
    ? "warning"
    : active || waitingQuestionCount
      ? "working"
      : "complete";
  const progressLabel = active
    ? failedQuestionCount
      ? "部分已完成，仍在生成…"
      : "研究图生成中…"
    : failedQuestionCount || waitingQuestionCount
      ? "部分已完成"
      : "已完成";
  const progressDetail = mapQuestions.length
    ? `${completedQuestionCount}/${mapQuestions.length} 个问题已完成${
      failedQuestionCount ? ` · ${failedQuestionCount} 个失败` : ""
    }${waitingQuestionCount ? ` · ${waitingQuestionCount} 个等待回答` : ""}`
    : active
      ? "正在规划问题与节点"
      : "图谱生成已结束";
  const requestedGenerationPending = requestedGeneration !== null && (
    !envelope?.paper_map
    || envelope.paper_map.generation < requestedGeneration
    || (
      envelope.paper_map.generation === requestedGeneration
      && envelope.paper_map.status !== "FAILED"
      && !envelope.paper_map.nodes.length
    )
  );
  const shouldPoll = active || requestedGenerationPending;

  useEffect(() => {
    if (!shouldPoll) return;
    const interval = window.setInterval(() => void load(), 1800);
    return () => window.clearInterval(interval);
  }, [load, shouldPoll]);

  useEffect(() => {
    const paperMap = envelope?.paper_map;
    if (!paperMap) {
      graphStructureSignatureRef.current = null;
      setNodes([]);
      setEdges([]);
      return;
    }
    const storedCollapsed = new Set(readStoredUi(paperId).collapsedNodeIds ?? []);
    const storedPositions = readStoredUi(paperId).positions;
    const mutationAnchorId = mutationLayoutAnchorRef.current;
    mutationLayoutAnchorRef.current = null;
    const nextStructureSignature = graphStructureSignature(paperMap.nodes, paperMap.edges);
    const structureChanged = graphStructureSignatureRef.current !== null
      && graphStructureSignatureRef.current !== nextStructureSignature;
    graphStructureSignatureRef.current = nextStructureSignature;
    const shouldFocusAfterLayout = mutationAnchorId !== null || structureChanged;
    collapsedNodeIdsRef.current = storedCollapsed;
    setCollapsedNodeIds(storedCollapsed);
    const hiddenNodeIds = hiddenDescendantIds(storedCollapsed, paperMap.edges);
    const childCounts = treeChildCounts(paperMap.edges);
    const nextEdges: Edge[] = paperMap.edges.map((edge) => flowEdgeFor(edge, hiddenNodeIds));
    edgesRef.current = nextEdges;
    setEdges(nextEdges);
    void layoutMap(
      paperMap.nodes,
      nextEdges,
      storedCollapsed.size || mutationAnchorId ? undefined : storedPositions,
      storedCollapsed,
      hiddenNodeIds,
      childCounts,
      (nodeId) => toggleNodeCollapseRef.current(nodeId),
      (nodeId) => openChildComposerRef.current(nodeId),
    )
      .then((arrangedNodes) => {
        const visibleAnchorId = mutationAnchorId ?? [...storedCollapsed].find(
          (nodeId) => !hiddenNodeIds.has(nodeId) && storedPositions?.[nodeId],
        );
        const arrangedAnchor = arrangedNodes.find((node) => node.id === visibleAnchorId);
        const storedAnchor = visibleAnchorId ? storedPositions?.[visibleAnchorId] : undefined;
        const offsetX = arrangedAnchor && storedAnchor
          ? storedAnchor.x - arrangedAnchor.position.x
          : 0;
        const offsetY = arrangedAnchor && storedAnchor
          ? storedAnchor.y - arrangedAnchor.position.y
          : 0;
        const nextNodes = arrangedNodes.map((node) => ({
          ...node,
          position: node.hidden
            ? storedPositions?.[node.id] ?? node.position
            : { x: node.position.x + offsetX, y: node.position.y + offsetY },
        }));
        nodesRef.current = nextNodes;
        setNodes(nextNodes);
        if (shouldFocusAfterLayout) focusSelectedItem(nextNodes, nextEdges);
      })
      .catch(() => setError("论文理解图布局失败，请刷新后重试。"));
  }, [envelope?.paper_map, focusSelectedItem, paperId, setEdges, setNodes]);

  const questionsById = useMemo(
    () => new Map(envelope?.paper_map?.questions.map((question) => [question.id, question]) ?? []),
    [envelope?.paper_map?.questions],
  );
  const selectedNode = envelope?.paper_map?.nodes.find((node) => node.id === selectedNodeId) ?? null;
  const deleteNode = envelope?.paper_map?.nodes.find((node) => node.id === deleteNodeId) ?? null;
  const selectedQuestion = selectedNode?.question_id
    ? questionsById.get(selectedNode.question_id) ?? null
    : null;
  const childComposerParent = envelope?.paper_map?.nodes.find(
    (node) => node.id === childComposer?.parentId,
  ) ?? null;
  const canChooseFromQuestionLibrary = childComposerParent?.node_type === "PAPER";

  const selectedChildCount = selectedNode
    ? envelope?.paper_map?.edges.filter(
      (edge) => edge.is_tree && edge.source_node_id === selectedNode.id,
    ).length ?? 0
    : 0;

  const selectedFlowEdge = edges.find((edge) => edge.id === selectedEdgeId) ?? null;
  const renderedNodes = useMemo<Node[]>(() => nodes.map((node) => {
    const endpointClass = selectedFlowEdge?.source === node.id
      ? " paper-map-edge-endpoint paper-map-edge-source"
      : selectedFlowEdge?.target === node.id
        ? " paper-map-edge-endpoint paper-map-edge-target"
        : "";
    return {
      ...node,
      selected: node.id === selectedNodeId,
      ...(endpointClass ? { className: `${node.className ?? ""}${endpointClass}` } : {}),
    };
  }), [nodes, selectedFlowEdge?.source, selectedFlowEdge?.target, selectedNodeId]);
  const renderedEdges = useMemo(() => edges.map((edge) => (
    edge.id === selectedEdgeId ? { ...edge, selected: true, zIndex: 2 } : edge
  )), [edges, selectedEdgeId]);

  const toggleNodeCollapse = useCallback(async (nodeId: string) => {
    const paperMap = paperMapRef.current;
    if (!paperMap) return;
    const nextCollapsed = new Set(collapsedNodeIdsRef.current);
    if (nextCollapsed.has(nodeId)) nextCollapsed.delete(nodeId);
    else nextCollapsed.add(nodeId);
    const hiddenNodeIds = hiddenDescendantIds(nextCollapsed, paperMap.edges);
    const childCounts = treeChildCounts(paperMap.edges);
    const nextEdges = edgesRef.current.map((edge) => ({
      ...edge,
      hidden: hiddenNodeIds.has(edge.source) || hiddenNodeIds.has(edge.target),
    }));
    const currentNodes = nodesRef.current;
    const anchorBefore = currentNodes.find((node) => node.id === nodeId)?.position;
    const layoutVersion = collapseLayoutVersionRef.current + 1;
    collapseLayoutVersionRef.current = layoutVersion;
    collapsedNodeIdsRef.current = nextCollapsed;
    setCollapsedNodeIds(nextCollapsed);
    saveStoredUi(paperId, { collapsedNodeIds: [...nextCollapsed] });
    try {
      const arranged = await layoutMap(
        paperMap.nodes,
        nextEdges,
        undefined,
        nextCollapsed,
        hiddenNodeIds,
        childCounts,
        (targetNodeId) => toggleNodeCollapseRef.current(targetNodeId),
        (targetNodeId) => openChildComposerRef.current(targetNodeId),
      );
      if (collapseLayoutVersionRef.current !== layoutVersion) return;
      const anchorAfter = arranged.find((node) => node.id === nodeId)?.position;
      const offsetX = anchorBefore && anchorAfter ? anchorBefore.x - anchorAfter.x : 0;
      const offsetY = anchorBefore && anchorAfter ? anchorBefore.y - anchorAfter.y : 0;
      const previousPositions = new Map(
        currentNodes.map((node) => [node.id, node.position]),
      );
      const compacted = arranged.map((node) => ({
        ...node,
        position: node.hidden
          ? previousPositions.get(node.id) ?? node.position
          : { x: node.position.x + offsetX, y: node.position.y + offsetY },
      }));
      nodesRef.current = compacted;
      edgesRef.current = nextEdges;
      setNodes(compacted);
      setEdges(nextEdges);
      saveStoredUi(paperId, {
        positions: Object.fromEntries(compacted.map((node) => [node.id, node.position])),
      });
      const selectedEdge = nextEdges.find((edge) => edge.id === selectedEdgeIdRef.current);
      if (
        (selectedNodeIdRef.current && hiddenNodeIds.has(selectedNodeIdRef.current))
        || selectedEdge?.hidden
      ) {
        selectedNodeIdRef.current = nodeId;
        selectedEdgeIdRef.current = null;
        setSelectedNodeId(nodeId);
        setSelectedEdgeId(null);
      }
      focusSelectedItem(compacted, nextEdges);
    } catch {
      setError("子图折叠布局失败，请重试。");
    }
  }, [focusSelectedItem, paperId, setEdges, setNodes]);

  toggleNodeCollapseRef.current = toggleNodeCollapse;

  const openChildComposer = useCallback((nodeId: string) => {
    selectedNodeIdRef.current = nodeId;
    selectedEdgeIdRef.current = null;
    setSelectedNodeId(nodeId);
    setSelectedEdgeId(null);
    setQuestionDraft("");
    setSelectedLibraryQuestionKey("");
    setQuestionLibraryError(null);
    setConceptType("CLAIM");
    setConceptTitle("");
    setConceptBody("");
    setChildComposer({ parentId: nodeId, mode: "CHOICE" });
  }, []);

  openChildComposerRef.current = openChildComposer;

  const openQuestionComposer = () => {
    if (!childComposer) return;
    const parentNode = envelope?.paper_map?.nodes.find(
      (node) => node.id === childComposer.parentId,
    );
    setQuestionDraft("");
    setSelectedLibraryQuestionKey("");
    setQuestionLibraryError(null);
    setChildComposer({ ...childComposer, mode: "QUESTION" });
    if (parentNode?.node_type !== "PAPER" || questionLibrary !== null || questionLibraryLoading) {
      return;
    }
    setQuestionLibraryLoading(true);
    void getPaperMapQuestionLibrary()
      .then((library) => setQuestionLibrary(library.questions))
      .catch((reason) => setQuestionLibraryError(
        reason instanceof Error ? reason.message : "暂时无法加载问题库，仍可直接输入问题。",
      ))
      .finally(() => setQuestionLibraryLoading(false));
  };

  const generateMap = async () => {
    setBusy(true);
    setError(null);
    setConfirmRegeneration(false);
    selectedNodeIdRef.current = null;
    selectedEdgeIdRef.current = null;
    setSelectedNodeId(null);
    setSelectedEdgeId(null);
    try {
      const result = await generatePaperVisualMap(paperId);
      setRequestedGeneration(result.generation);
      await load();
      setCollapsed(false);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法创建论文理解图。");
    } finally {
      setBusy(false);
    }
  };

  const answerQuestion = async (question: PaperMapQuestion) => {
    setBusy(true);
    setError(null);
    try {
      await generatePaperMapAnswer(question.id);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法生成这个问题的回答。");
    } finally {
      setBusy(false);
    }
  };

  const createQuestionChild = async () => {
    if (!childComposer || questionDraft.trim().length < 3) return;
    const parentId = childComposer.parentId;
    setBusy(true);
    setError(null);
    try {
      await createPaperMapFollowup(parentId, questionDraft.trim());
      saveStoredUi(paperId, {
        positions: Object.fromEntries(nodesRef.current.map((node) => [node.id, node.position])),
      });
      mutationLayoutAnchorRef.current = parentId;
      setQuestionDraft("");
      setChildComposer(null);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法创建提问节点。");
    } finally {
      setBusy(false);
    }
  };

  const createConceptChild = async () => {
    if (!childComposer || !conceptTitle.trim()) return;
    const parentId = childComposer.parentId;
    setBusy(true);
    setError(null);
    try {
      await createPaperMapConcept(parentId, {
        concept_type: conceptType,
        label: conceptTitle.trim(),
        body: conceptBody.trim(),
      });
      saveStoredUi(paperId, {
        positions: Object.fromEntries(nodesRef.current.map((node) => [node.id, node.position])),
      });
      mutationLayoutAnchorRef.current = parentId;
      setConceptTitle("");
      setConceptBody("");
      setChildComposer(null);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法创建 Concept 节点。");
    } finally {
      setBusy(false);
    }
  };

  const connectNodes = useCallback(async (connection: Connection) => {
    const paperMap = paperMapRef.current;
    if (!paperMap || !connection.source || !connection.target || connection.source === connection.target) return;
    setBusy(true);
    setError(null);
    try {
      const created = await createPaperMapEdge(paperMap.id, connection.source, connection.target);
      selectedNodeIdRef.current = null;
      selectedEdgeIdRef.current = created.edge.id;
      setSelectedNodeId(null);
      setSelectedEdgeId(created.edge.id);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法创建节点连边。");
    } finally {
      setBusy(false);
    }
  }, [load]);

  const editEdge: EdgeMouseHandler = (_event, edge) => {
    selectedNodeIdRef.current = null;
    selectedEdgeIdRef.current = edge.id;
    setSelectedNodeId(null);
    setSelectedEdgeId(edge.id);
    setEdgeEditor({
      edgeId: edge.id,
      label: typeof edge.label === "string" ? edge.label : "",
    });
  };

  const saveEdgeLabel = async () => {
    if (!edgeEditor) return;
    setBusy(true);
    setError(null);
    try {
      await updatePaperMapEdge(edgeEditor.edgeId, edgeEditor.label);
      setEdgeEditor(null);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法保存连边文字。");
    } finally {
      setBusy(false);
    }
  };

  const removeEdge = useCallback(async (edgeId: string) => {
    setBusy(true);
    setError(null);
    try {
      await deletePaperMapEdge(edgeId);
      selectedEdgeIdRef.current = null;
      setSelectedEdgeId(null);
      setEdgeEditor(null);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法删除连边。");
    } finally {
      setBusy(false);
    }
  }, [load]);

  const confirmNodeDelete = async () => {
    if (!deleteNodeId) return;
    const paperMap = paperMapRef.current;
    const anchorId = paperMap?.edges.find(
      (edge) => edge.is_tree && edge.target_node_id === deleteNodeId,
    )?.source_node_id ?? null;
    setBusy(true);
    setError(null);
    try {
      saveStoredUi(paperId, {
        positions: Object.fromEntries(nodesRef.current.map((node) => [node.id, node.position])),
      });
      mutationLayoutAnchorRef.current = anchorId;
      await deletePaperMapNode(deleteNodeId);
      setDeleteNodeId(null);
      selectedNodeIdRef.current = anchorId;
      selectedEdgeIdRef.current = null;
      setSelectedNodeId(anchorId);
      setSelectedEdgeId(null);
      setChildComposer(null);
      await load();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "无法删除节点。");
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    const handleDeleteKey = (event: KeyboardEvent) => {
      if (event.key !== "Delete" || busy) return;
      const target = event.target as HTMLElement | null;
      if (target?.matches("input, textarea, select, [contenteditable='true']")) return;
      if (selectedEdgeId) {
        event.preventDefault();
        void removeEdge(selectedEdgeId);
      } else if (selectedNodeId) {
        event.preventDefault();
        setDeleteNodeId(selectedNodeId);
      }
    };
    window.addEventListener("keydown", handleDeleteKey);
    return () => window.removeEventListener("keydown", handleDeleteKey);
  }, [busy, removeEdge, selectedEdgeId, selectedNodeId]);

  const autoLayout = async () => {
    const paperMap = envelope?.paper_map;
    if (!paperMap) return;
    setBusy(true);
    try {
      const hiddenNodeIds = hiddenDescendantIds(collapsedNodeIdsRef.current, paperMap.edges);
      const arranged = await layoutMap(
        paperMap.nodes,
        edges,
        undefined,
        collapsedNodeIdsRef.current,
        hiddenNodeIds,
        treeChildCounts(paperMap.edges),
        (nodeId) => toggleNodeCollapseRef.current(nodeId),
        (nodeId) => openChildComposerRef.current(nodeId),
      );
      nodesRef.current = arranged;
      setNodes(arranged);
      saveStoredUi(paperId, {
        positions: Object.fromEntries(arranged.map((node) => [node.id, node.position])),
      });
      if (!focusSelectedItem(arranged, edgesRef.current)) {
        window.setTimeout(() => flowRef.current?.fitView({ padding: 0.15, duration: 450 }), 0);
      }
    } catch {
      setError("自动排布失败，请稍后重试。");
    } finally {
      setBusy(false);
    }
  };

  const toggleCollapsed = () => {
    const next = !collapsed;
    setCollapsed(next);
    saveStoredUi(paperId, { collapsed: next });
  };

  const onNodeClick: NodeMouseHandler = (_event, node) => {
    selectedNodeIdRef.current = node.id;
    selectedEdgeIdRef.current = null;
    setSelectedNodeId(node.id);
    setSelectedEdgeId(null);
  };

  const onEdgeClick: EdgeMouseHandler = (_event, edge) => {
    selectedNodeIdRef.current = null;
    selectedEdgeIdRef.current = edge.id;
    setSelectedNodeId(null);
    setSelectedEdgeId(edge.id);
  };

  const clearSelection = () => {
    selectedNodeIdRef.current = null;
    selectedEdgeIdRef.current = null;
    setSelectedNodeId(null);
    setSelectedEdgeId(null);
  };

  const onNodeDragStop = (
    _event: MouseEvent | TouchEvent,
    _node: Node,
    draggedNodes: Node[],
  ) => {
    const changed = draggedNodes.length ? draggedNodes : nodes;
    const positions = Object.fromEntries(nodes.map((node) => [node.id, node.position]));
    for (const node of changed) positions[node.id] = node.position;
    saveStoredUi(paperId, { positions });
  };

  const onMoveEnd: OnMoveEnd = (_event, viewport) => {
    saveStoredUi(paperId, { viewport });
  };

  const beginResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    event.currentTarget.setPointerCapture(event.pointerId);
    resizeStart.current = { y: event.clientY, height };
  };

  const resize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!resizeStart.current) return;
    setHeight(Math.min(900, Math.max(340, resizeStart.current.height + event.clientY - resizeStart.current.y)));
  };

  const finishResize = (event: ReactPointerEvent<HTMLDivElement>) => {
    if (!resizeStart.current) return;
    event.currentTarget.releasePointerCapture(event.pointerId);
    resizeStart.current = null;
    saveStoredUi(paperId, { height });
  };

  return (
    <section className={`paper-visual-map ${collapsed ? "paper-visual-map-collapsed" : ""}`}>
      <header className="paper-map-header">
        <button
          aria-label={collapsed ? "展开论文理解图" : "折叠论文理解图"}
          className="paper-map-collapse"
          onClick={toggleCollapsed}
          type="button"
        >
          {collapsed ? "▸" : "▾"}
        </button>
        <div>
          <p className="eyebrow">Paper visual insight map</p>
          <h2>论文理解图</h2>
        </div>
        <div className="paper-map-header-actions">
          {envelope?.paper_map?.primary_type ? (
            <span>{envelope.paper_map.primary_type.replaceAll("_", " ")}</span>
          ) : null}
          {!collapsed && envelope?.configured ? (
            <>
              {envelope?.paper_map?.nodes.length ? (
                <button disabled={busy} onClick={() => void autoLayout()} type="button">自动整理</button>
              ) : null}
              <button
                disabled={busy || active || requestedGenerationPending}
                onClick={() => {
                  if (envelope?.paper_map) setConfirmRegeneration(true);
                  else void generateMap();
                }}
                type="button"
              >
                {envelope?.paper_map ? "重新生成" : "生成理解图"}
              </button>
            </>
          ) : null}
        </div>
      </header>

      {!collapsed ? (
        <>
          <div className="paper-map-body" ref={flowContainerRef} style={{ height }}>
            {!envelope ? <p className="paper-map-empty">正在加载…</p> : null}
            {envelope && !envelope.configured ? (
              <p className="paper-map-empty">当前未配置 AI Provider，理解图暂不可生成。</p>
            ) : null}
            {envelope?.configured && !envelope.paper_map ? (
              <div className="paper-map-empty">
                <strong>从问题出发，快速建立对这篇论文的整体认识。</strong>
                <p>
                  系统会先识别论文类型并语义筛选 4–10 个适用问题，再逐步补充有原文证据的回答。
                  请注意：理解图的生成通常需要较长时间，并会消耗较多 Token；实际耗时和 Token
                  消耗量难以准确预测，请根据需要谨慎使用。
                </p>
                <button disabled={busy} onClick={generateMap} type="button">开始生成</button>
              </div>
            ) : null}
            {envelope?.paper_map && !envelope.paper_map.nodes.length ? (
              <div className="paper-map-empty">
                <strong>{envelope.paper_map.status === "FAILED" ? "生成失败" : "正在规划阅读问题…"}</strong>
                {envelope.paper_map.error_message ? <p role="alert">{envelope.paper_map.error_message}</p> : null}
                {envelope.paper_map.status === "FAILED" ? (
                  <button disabled={busy} onClick={generateMap} type="button">显式重试</button>
                ) : null}
              </div>
            ) : null}
            {nodes.length ? (
              <ReactFlow
                connectionMode={ConnectionMode.Strict}
                defaultViewport={initialUi.viewport ?? { x: 24, y: 24, zoom: 0.72 }}
                deleteKeyCode={null}
                edges={renderedEdges}
                fitView={!initialUi.viewport}
                fitViewOptions={{ padding: 0.15 }}
                nodes={renderedNodes}
                onConnect={(connection) => void connectNodes(connection)}
                onEdgeClick={onEdgeClick}
                onEdgeDoubleClick={editEdge}
                onEdgesChange={onEdgesChange}
                onMoveEnd={onMoveEnd}
                onInit={(instance) => { flowRef.current = instance; }}
                onNodeClick={onNodeClick}
                onNodeDragStop={onNodeDragStop}
                onNodesChange={onNodesChange}
                onPaneClick={clearSelection}
                panOnDrag
                zoomOnPinch
                zoomOnScroll
              >
                <Background color="#6c7f72" gap={24} size={1} />
                <Panel position="top-left">
                  <div
                    aria-live="polite"
                    className={`paper-map-generation-state paper-map-generation-${progressTone}${
                      active ? " paper-map-generation-active" : ""
                    }`}
                  >
                    <span aria-hidden="true" className="paper-map-generation-dot" />
                    <span>
                      <strong>{progressLabel}</strong>
                      <small>{progressDetail}</small>
                    </span>
                  </div>
                </Panel>
                <Controls position="bottom-left" />
                <MiniMap nodeStrokeWidth={3} pannable zoomable />
              </ReactFlow>
            ) : null}

            {selectedNode ? (
              <aside className="paper-map-inspector" aria-label="理解图节点详情">
                <button
                  aria-label="关闭节点详情"
                  className="paper-map-inspector-close"
                  onClick={clearSelection}
                  type="button"
                >×</button>
                <span>{selectedNode.node_type === "CONCEPT" && selectedNode.concept_type
                  ? `CONCEPT: ${selectedNode.concept_type.replaceAll("_", " ")}`
                  : selectedNode.node_type.replaceAll("_", " ")}</span>
                <h3>{selectedNode.label}</h3>
                <small className="paper-map-delete-hint">按 Delete 可删除此节点</small>
                {selectedChildCount ? (
                  <button
                    className="paper-map-inspector-collapse"
                    onClick={() => toggleNodeCollapse(selectedNode.id)}
                    type="button"
                  >
                    {collapsedNodeIds.has(selectedNode.id)
                      ? `展开 ${selectedChildCount} 个子分支`
                      : `折叠 ${selectedChildCount} 个子分支`}
                  </button>
                ) : null}
                {selectedNode.node_type !== "QUESTION" && selectedNode.body ? <p>{selectedNode.body}</p> : null}
                {selectedQuestion ? (
                  <div className="paper-map-question-answer">
                    <small>{questionStateLabel(selectedQuestion)}</small>
                    {selectedNode.node_type === "QUESTION" && selectedQuestion.rationale ? (
                      <p className="paper-map-rationale">{selectedQuestion.rationale}</p>
                    ) : null}
                    {selectedNode.node_type === "QUESTION" && selectedQuestion.answer_text ? (
                      <p>{selectedQuestion.answer_text}</p>
                    ) : null}
                    {selectedQuestion.status === "FAILED" ? (
                      <p role="alert">{selectedQuestion.error_message ?? "回答生成失败。"}</p>
                    ) : null}
                    {selectedNode.node_type === "QUESTION" ? (
                      <button
                        disabled={busy || ["QUEUED", "RUNNING"].includes(selectedQuestion.status)}
                        onClick={() => void answerQuestion(selectedQuestion)}
                        type="button"
                      >
                        {selectedQuestion.status === "FAILED" ? "重试回答" : selectedQuestion.status === "SUCCEEDED" ? "重新回答" : "回答这个问题"}
                      </button>
                    ) : null}
                  </div>
                ) : null}
                {(selectedNode.node_type === "QUESTION"
                  ? selectedQuestion?.citations ?? []
                  : selectedNode.evidence).length ? (
                  <div className="paper-map-evidence">
                    <strong>原文证据</strong>
                    {(selectedNode.node_type === "QUESTION"
                      ? selectedQuestion?.citations ?? []
                      : selectedNode.evidence).map((citation, index) => (
                      <button
                        key={`${citation.block_id}-${index}`}
                        onClick={() => jumpToPaperBlock(citation.block_id)}
                        type="button"
                      >
                        “{citation.quote}”
                      </button>
                    ))}
                  </div>
                ) : null}
              </aside>
            ) : null}

            {selectedEdgeId && !edgeEditor ? (
              <div className="paper-map-selection-tip" role="status">
                已选择连边 · 双击编辑文字 · Delete 删除
              </div>
            ) : null}

            {childComposer ? (
              <aside aria-label="新增子节点" className="paper-map-dialog paper-map-child-composer" role="dialog">
                <button
                  aria-label="关闭新增节点窗口"
                  className="paper-map-dialog-close"
                  onClick={() => setChildComposer(null)}
                  type="button"
                >×</button>
                <span>新增子节点</span>
                {childComposer.mode === "CHOICE" ? (
                  <>
                    <h3>选择节点类型</h3>
                    <div className="paper-map-child-type-actions">
                      <button
                        onClick={openQuestionComposer}
                        type="button"
                      >向 AI 提问</button>
                      <button
                        onClick={() => setChildComposer({ ...childComposer, mode: "CONCEPT" })}
                        type="button"
                      >记录灵感和概念</button>
                    </div>
                  </>
                ) : null}
                {childComposer.mode === "QUESTION" ? (
                  <form onSubmit={(event) => { event.preventDefault(); void createQuestionChild(); }}>
                    <h3>向 AI 提问</h3>
                    {canChooseFromQuestionLibrary ? (
                      <label className="paper-map-question-picker">
                        问题库（可选）
                        <select
                          aria-label="从问题库选择问题"
                          disabled={questionLibraryLoading}
                          onChange={(event) => {
                            const key = event.target.value;
                            const selected = questionLibrary?.find((item) => item.key === key);
                            setSelectedLibraryQuestionKey(key);
                            if (selected) setQuestionDraft(selected.text);
                          }}
                          value={selectedLibraryQuestionKey}
                        >
                          <option value="">选择问题，或在下方直接输入…</option>
                          {(questionLibrary ?? []).map((item) => (
                            <option key={item.key} value={item.key}>
                              {item.user_defined ? `自定义 · ${item.text}` : item.text}
                            </option>
                          ))}
                        </select>
                        {questionLibraryLoading ? <small role="status">正在加载问题库…</small> : null}
                        {questionLibraryError ? <small role="alert">{questionLibraryError}</small> : null}
                      </label>
                    ) : null}
                    <p className="paper-map-question-input-hint">
                      {canChooseFromQuestionLibrary
                        ? "可以从问题库选择并继续修改，也可以直接输入自己的问题。"
                        : "输入一个与当前节点相关的问题。"}
                    </p>
                    <textarea
                      aria-label="新子节点问题"
                      autoFocus
                      maxLength={2000}
                      onChange={(event) => {
                        setQuestionDraft(event.target.value);
                        setSelectedLibraryQuestionKey("");
                      }}
                      placeholder="输入要向 AI 提出的问题…"
                      rows={4}
                      value={questionDraft}
                    />
                    <div className="paper-map-dialog-actions">
                      <button onClick={() => setChildComposer({ ...childComposer, mode: "CHOICE" })} type="button">返回</button>
                      <button disabled={busy || questionDraft.trim().length < 3} type="submit">创建并回答</button>
                    </div>
                  </form>
                ) : null}
                {childComposer.mode === "CONCEPT" ? (
                  <form onSubmit={(event) => { event.preventDefault(); void createConceptChild(); }}>
                    <h3>创建 Concept 子节点</h3>
                    <label>
                      类型
                      <select
                        aria-label="Concept 类型"
                        onChange={(event) => setConceptType(event.target.value as (typeof CONCEPT_TYPES)[number])}
                        value={conceptType}
                      >
                        {CONCEPT_TYPES.map((type) => <option key={type} value={type}>{type}</option>)}
                      </select>
                    </label>
                    <label>
                      标题
                      <input
                        aria-label="Concept 标题"
                        autoFocus
                        maxLength={240}
                        onChange={(event) => setConceptTitle(event.target.value)}
                        value={conceptTitle}
                      />
                    </label>
                    <label>
                      正文
                      <textarea
                        aria-label="Concept 正文"
                        maxLength={8000}
                        onChange={(event) => setConceptBody(event.target.value)}
                        rows={5}
                        value={conceptBody}
                      />
                    </label>
                    <div className="paper-map-dialog-actions">
                      <button onClick={() => setChildComposer({ ...childComposer, mode: "CHOICE" })} type="button">返回</button>
                      <button disabled={busy || !conceptTitle.trim()} type="submit">创建 Concept</button>
                    </div>
                  </form>
                ) : null}
              </aside>
            ) : null}

            {edgeEditor ? (
              <form
                aria-label="编辑连边文字"
                className="paper-map-dialog paper-map-edge-editor"
                onSubmit={(event) => { event.preventDefault(); void saveEdgeLabel(); }}
                role="dialog"
              >
                <button
                  aria-label="关闭连边编辑窗口"
                  className="paper-map-dialog-close"
                  onClick={() => setEdgeEditor(null)}
                  type="button"
                >×</button>
                <span>连边文字</span>
                <h3>描述这条关系</h3>
                <input
                  aria-label="连边文字"
                  autoFocus
                  maxLength={160}
                  onChange={(event) => setEdgeEditor({ ...edgeEditor, label: event.target.value })}
                  placeholder="留空则不显示文字和底色"
                  value={edgeEditor.label}
                />
                <div className="paper-map-dialog-actions">
                  <button onClick={() => setEdgeEditor(null)} type="button">取消</button>
                  <button disabled={busy} type="submit">保存</button>
                </div>
              </form>
            ) : null}

            {confirmRegeneration && envelope?.paper_map ? (
              <aside
                aria-label="确认重新生成论文理解图"
                className="paper-map-dialog paper-map-regenerate-confirm"
                role="dialog"
              >
                <span>重新生成理解图</span>
                <h3>确定要重新生成当前论文理解图吗？</h3>
                <p>
                  系统会创建一个新的图谱 generation 并重新调用 AI。当前图不会被物理删除，
                  但新图完成后会取代它成为阅读页中的当前版本；当前图中的手动节点不会自动复制。
                </p>
                <div className="paper-map-dialog-actions">
                  <button
                    disabled={busy}
                    onClick={() => setConfirmRegeneration(false)}
                    type="button"
                  >
                    取消
                  </button>
                  <button
                    className="danger"
                    disabled={busy}
                    onClick={() => void generateMap()}
                    type="button"
                  >
                    {busy ? "正在提交…" : "确认重新生成"}
                  </button>
                </div>
              </aside>
            ) : null}

            {deleteNode ? (
              <aside aria-label="确认删除节点" className="paper-map-dialog paper-map-delete-confirm" role="dialog">
                <span>删除节点</span>
                <h3>确定删除“{deleteNode.label}”吗？</h3>
                <p>该节点及其直接依赖的数据会从当前论文理解图中移除。此操作不会使用浏览器弹窗。</p>
                <div className="paper-map-dialog-actions">
                  <button onClick={() => setDeleteNodeId(null)} type="button">取消</button>
                  <button className="danger" disabled={busy} onClick={() => void confirmNodeDelete()} type="button">确认删除</button>
                </div>
              </aside>
            ) : null}
          </div>
          {error ? <p className="paper-map-error" role="alert">{error}</p> : null}
          <div
            aria-label="拖动调整论文理解图高度"
            className="paper-map-resizer"
            onPointerDown={beginResize}
            onPointerMove={resize}
            onPointerUp={finishResize}
            role="separator"
          ><span /></div>
        </>
      ) : null}
    </section>
  );
}
