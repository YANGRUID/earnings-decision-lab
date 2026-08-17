"""Tests for tools that query real (currently empty) tables — verifying
they report honestly rather than fabricating results. See
agents/tools/options_snapshot.py and agents/tools/strategy_replay.py.
"""

from agents.tools.options_snapshot import OptionsSnapshotArgs, OptionsSnapshotTool
from agents.tools.strategy_replay import StrategyReplayArgs, StrategyReplayTool
from models.company import Company


def test_options_snapshot_reports_no_data_honestly(db_session):
    company = Company(ticker="ZZAGT6", name="ZZ Agent Test 6", cik="0009990006")
    db_session.add(company)
    db_session.flush()

    tool = OptionsSnapshotTool(db_session)
    outcome = tool.run(OptionsSnapshotArgs(ticker="ZZAGT6"))

    assert outcome.success
    assert outcome.data["snapshots"] == []
    assert "No options chain data is available" in outcome.summary


def test_strategy_replay_reports_no_data_honestly(db_session):
    company = Company(ticker="ZZAGT7", name="ZZ Agent Test 7", cik="0009990007")
    db_session.add(company)
    db_session.flush()

    tool = StrategyReplayTool(db_session)
    outcome = tool.run(StrategyReplayArgs(ticker="ZZAGT7"))

    assert outcome.success
    assert outcome.data["replays"] == []
    assert "No historical strategy-replay results" in outcome.summary


def test_options_snapshot_unknown_ticker(db_session):
    tool = OptionsSnapshotTool(db_session)
    outcome = tool.run(OptionsSnapshotArgs(ticker="NOSUCHTICKER"))
    assert outcome.success
    assert outcome.data["snapshots"] == []


def test_strategy_replay_unknown_ticker(db_session):
    tool = StrategyReplayTool(db_session)
    outcome = tool.run(StrategyReplayArgs(ticker="NOSUCHTICKER"))
    assert outcome.success
    assert outcome.data["replays"] == []
