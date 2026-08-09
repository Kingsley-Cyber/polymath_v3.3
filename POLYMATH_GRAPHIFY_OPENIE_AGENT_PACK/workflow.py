from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass(frozen=True)
class WorkflowNode:
    name: str
    title: str
    stage_module: str
    depends_on: List[str] = field(default_factory=list)
    required_artifacts: List[str] = field(default_factory=list)
    validates: List[str] = field(default_factory=list)
    description: str = ""
    kill_switch: bool = False


WORKFLOW: List[WorkflowNode] = [
    WorkflowNode(
        name="DISCOVER_REPOSITORY",
        title="Discover repository capabilities and seams",
        stage_module="stages.discover_repo",
        required_artifacts=["work/repository_map.json", "work/repository_map.md"],
        validates=["repository_map_exists"],
        description="Map Graphify, extraction, model providers, spaCy, predicate compiler, stores, and tests to actual repository paths.",
    ),
    WorkflowNode(
        name="CAPTURE_BASELINE",
        title="Capture current Relex/Graphify baseline",
        stage_module="stages.capture_baseline",
        depends_on=["DISCOVER_REPOSITORY"],
        required_artifacts=["work/baseline/relex_baseline_manifest.json"],
        description="Run current executable baseline on fixtures and preserve timing, entity, relation, graph, and evidence artifacts.",
    ),
    WorkflowNode(
        name="RUN_RELATION_KILL_SWITCH",
        title="Run gold-entity relation ceiling test",
        stage_module="stages.syntax_ceiling",
        depends_on=["CAPTURE_BASELINE"],
        required_artifacts=["work/metrics/relation_kill_switch.json"],
        description="Compare existing syntax, triplet-extract, and combined OpenIE + precision rules on gold entity spans.",
        kill_switch=True,
    ),
    WorkflowNode("RUN_CORPUS_SURVEY", "Run zero-model corpus survey", "stages.corpus_survey", ["RUN_RELATION_KILL_SWITCH"], ["work/survey/corpus_survey.json"], description="Build deterministic furniture, headings, alias/acronym, rare-term, and gazetteer-candidate inventory."),
    WorkflowNode("IMPLEMENT_CPU_ENTITY_PROVIDER", "Implement CPU-only GLiNER2 provider", "stages.entity_provider", ["RUN_CORPUS_SURVEY"], ["work/validation/cpu_only_provider.json"], validates=["cpu_only"], description="Wire fastino/gliner2-base-v1 CPU-only provider; fail on MPS/MLX/CUDA."),
    WorkflowNode("IMPLEMENT_ENTITY_CENSUS", "Implement corpus-batched entity census", "stages.entity_census", ["IMPLEMENT_CPU_ENTITY_PROVIDER"], ["work/artifacts/raw_mentions.jsonl"], validates=["raw_mention_conservation"], description="Batch GLiNER2 windows, persist immutable raw mentions before gates."),
    WorkflowNode("IMPLEMENT_ENTITY_REDUCER", "Implement document entity reducer", "stages.entity_reduce", ["IMPLEMENT_ENTITY_CENSUS"], ["work/artifacts/document_entities.jsonl"], validates=["entity_reducer_conservation"], description="Aggregate mention -> document entity -> optional corpus entity using recurrence, definitions, aliases, genericity, and type purity."),
    WorkflowNode("IMPLEMENT_MENTION_COMPLETION", "Implement document-local mention completion", "stages.mention_complete", ["IMPLEMENT_ENTITY_REDUCER"], ["work/artifacts/completed_mentions.jsonl"], validates=["exact_offsets"], description="Use PhraseMatcher / EntityRuler over promoted document entities; preserve every mention offset."),
    WorkflowNode("IMPLEMENT_RELATION_ELIGIBILITY", "Implement relation eligibility router", "stages.relation_eligibility", ["IMPLEMENT_MENTION_COMPLETION"], ["work/artifacts/relation_eligibility.jsonl"], description="Persist relation-eligible and ineligible decisions without discarding source text."),
    WorkflowNode("INTEGRATE_TRIPLET_EXTRACT", "Integrate triplet-extract OpenIE", "stages.openie_extract", ["IMPLEMENT_RELATION_ELIGIBILITY"], ["work/artifacts/openie_raw_propositions.jsonl"], validates=["parse_once"], description="Run triplet-extract Balanced CPU on eligible sentences/windows; no graph writes."),
    WorkflowNode("IMPLEMENT_ARGUMENT_ADAPTER", "Implement OpenIE Argument Adapter", "stages.argument_adapter", ["INTEGRATE_TRIPLET_EXTRACT"], ["work/artifacts/adapted_arguments.jsonl"], validates=["exact_offsets"], description="Classify arguments as ENTITY, LITERAL, DESCRIPTION, EMBEDDED_CLAUSE, or UNRESOLVED."),
    WorkflowNode("IMPLEMENT_PROPOSITION_REDUCER", "Implement OpenIE Proposition Reducer", "stages.proposition_reduce", ["IMPLEMENT_ARGUMENT_ADAPTER"], ["work/artifacts/proposition_families.jsonl"], validates=["conservation"], description="Collapse entailed duplicates while retaining supporting renderings."),
    WorkflowNode("WIRE_PREDICATE_COMPILER", "Wire existing predicate compiler", "stages.predicate_compile", ["IMPLEMENT_PROPOSITION_REDUCER"], ["work/artifacts/predicate_candidates.jsonl"], description="Map surface relations to bounded ontology or STORE_UNMAPPED_SURFACE_RELATION."),
    WorkflowNode("WIRE_ASSERTION_GATE", "Wire claim/assertion assembler and gate", "stages.assertion_validate", ["WIRE_PREDICATE_COMPILER"], ["work/artifacts/assertion_decisions.jsonl"], description="Emit FACT, QUALIFIED_CLAIM, OPEN_RELATION, REVIEW, and REJECT lanes."),
    WorkflowNode("RUN_TWO_DOCUMENT_E2E", "Run two-document end-to-end Graphify test", "stages.graph_project", ["WIRE_ASSERTION_GATE"], ["work/metrics/e2e_metrics.json"], validates=["acceptance_gates"], description="Graphify quality and throughput fixtures through isolated persistence/projection."),
    WorkflowNode("REBUILD_GRAPH_FROM_CANONICAL_ARTIFACTS", "Rebuild isolated graph projection", "stages.graph_rebuild", ["RUN_TWO_DOCUMENT_E2E"], ["work/metrics/graph_rebuild.json"], validates=["graph_idempotency"], description="Delete test projection, rebuild from Mongo/canonical artifacts, compare identities and counts."),
    WorkflowNode("RUN_IDEMPOTENCY_TEST", "Run idempotency test", "stages.final_verify", ["REBUILD_GRAPH_FROM_CANONICAL_ARTIFACTS"], ["work/metrics/idempotency.json"], validates=["graph_idempotency"], description="Run fixtures twice; ensure no duplicates across mentions, entities, assertions, nodes, or edges."),
    WorkflowNode("COMPARE_TO_BASELINE", "Compare refactor to baseline", "stages.final_verify", ["RUN_IDEMPOTENCY_TEST"], ["work/metrics/baseline_comparison.json"], validates=["acceptance_gates"], description="Measure quality and speed deltas against captured Relex baseline."),
    WorkflowNode("FINAL_VERIFY", "Final verification and report", "stages.final_verify", ["COMPARE_TO_BASELINE"], ["work/reports/final_status.md", "work/reports/final_status.json"], validates=["provider_reachability", "acceptance_gates"], description="Produce final pass/fail status and remaining uncertainties."),
]

WORKFLOW_BY_NAME: Dict[str, WorkflowNode] = {node.name: node for node in WORKFLOW}
