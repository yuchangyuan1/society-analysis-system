"""Agents (v2) - redesign-2026-05.

v1 agents (counter_message, visual, critic, risk, community, analysis,
report, planner, router, precompute_pipeline) were deleted in Phase 5
cleanup. The v2 layer is in this file's siblings.
"""
from .ingestion import IngestionAgent
from .knowledge import KnowledgeAgent
from .multimodal_agent import MultimodalAgent
from .entity_extractor import EntityExtractor
from .official_ingestion_pipeline import OfficialIngestionPipeline
from .schema_agent import SchemaAgent
from .topic_clusterer import TopicClusterer
from .post_dedup import PostDeduper
from .precompute_pipeline_v2 import PrecomputePipelineV2
from .query_rewriter import QueryRewriter
from .planner_v2 import BoundedPlannerV2, PlanExecutionV2
from .report_writer import ReportWriter
from .quality_critic import QualityCritic
from .ablation_runner import AblationContext, AblationRunner
from .chat_orchestrator import ChatOrchestrator

__all__ = [
    "IngestionAgent",
    "KnowledgeAgent",
    "MultimodalAgent",
    "EntityExtractor",
    "OfficialIngestionPipeline",
    "SchemaAgent",
    "TopicClusterer",
    "PostDeduper",
    "PrecomputePipelineV2",
    "QueryRewriter",
    "BoundedPlannerV2",
    "PlanExecutionV2",
    "ReportWriter",
    "QualityCritic",
    "AblationContext",
    "AblationRunner",
    "ChatOrchestrator",
]
