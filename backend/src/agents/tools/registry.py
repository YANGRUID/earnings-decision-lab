from sqlalchemy.orm import Session

from agents.tools.base import Tool
from agents.tools.earnings_history import EarningsHistoryTool
from agents.tools.filings_search import FilingsSearchTool
from agents.tools.guidance_comparison import GuidanceComparisonTool
from agents.tools.implied_move import ImpliedMoveTool
from agents.tools.options_payoff import OptionsPayoffTool
from agents.tools.options_snapshot import OptionsSnapshotTool
from agents.tools.strategy_replay import StrategyReplayTool
from rag.embeddings import EmbeddingProvider


def build_tool_registry(db: Session, embedder: EmbeddingProvider) -> dict[str, Tool]:
    tools: list[Tool] = [
        EarningsHistoryTool(db),
        FilingsSearchTool(db, embedder),
        GuidanceComparisonTool(db),
        OptionsPayoffTool(),
        ImpliedMoveTool(),
        OptionsSnapshotTool(db),
        StrategyReplayTool(db),
    ]
    return {tool.name: tool for tool in tools}
