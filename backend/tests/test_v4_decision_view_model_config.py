"""V4 DecisionView model configuration (2026-09-02): explicit, fail-closed,
persisted as provenance, and never a silent fallback.

Deterministic: the LLM is replaced by a fake provider that records what it
was asked for and returns a canned structured view plus response metadata.
No network, no production database, no shadow evidence.
"""

from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from core.config import Settings
from services.llm.errors import LLMConfigurationError, StructuredOutputError
from services.llm.types import GenerateResult, TokenUsage
from services.v4_decision_view_config import (
    V4_DECISION_VIEW_CONFIG_VERSION,
    V4DecisionViewConfigError,
    describe_v4_decision_view_config,
    resolve_v4_decision_view_config,
)


def _settings(**overrides) -> Settings:
    base = {
        "llm_provider": "deepseek",
        "deepseek_api_key": "test-key",
        "deepseek_model": "deepseek-v4-flash",
        "v4_decision_view_model": "deepseek-v4-pro",
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


class TestConfigurationParsing:
    def test_explicit_pro_with_thinking_high_resolves(self):
        cfg = resolve_v4_decision_view_config(_settings())
        assert cfg.provider == "deepseek"
        assert cfg.model == "deepseek-v4-pro"
        assert cfg.thinking == "enabled"
        assert cfg.reasoning_effort == "high"
        assert cfg.max_tokens == 16384
        assert cfg.config_version == V4_DECISION_VIEW_CONFIG_VERSION

    def test_defaults_are_thinking_enabled_and_effort_high(self):
        s = Settings(_env_file=None)
        assert s.v4_decision_view_thinking == "enabled"
        assert s.v4_decision_view_reasoning_effort == "high"

    @pytest.mark.parametrize("bad", ["ultra", "medium", "HIGH", "", "1"])
    def test_unsupported_reasoning_effort_is_a_startup_configuration_error(self, bad):
        with pytest.raises(ValidationError):
            _settings(v4_decision_view_reasoning_effort=bad)

    @pytest.mark.parametrize("bad", ["yes", "true", "on", "1", "ENABLED"])
    def test_invalid_thinking_value_is_a_startup_configuration_error(self, bad):
        with pytest.raises(ValidationError):
            _settings(v4_decision_view_thinking=bad)

    def test_missing_model_fails_closed_with_no_fallback_to_deepseek_model(self):
        s = _settings(v4_decision_view_model=None)
        with pytest.raises(V4DecisionViewConfigError, match="no fallback to DEEPSEEK_MODEL"):
            resolve_v4_decision_view_config(s)
        assert describe_v4_decision_view_config(s)["config_error"] is not None
        assert describe_v4_decision_view_config(s)["model"] is None  # never shows flash

    def test_non_deepseek_provider_fails_closed(self):
        s = _settings(llm_provider="openai", openai_api_key="k", openai_model="gpt")
        with pytest.raises(V4DecisionViewConfigError, match="LLM_PROVIDER=deepseek"):
            resolve_v4_decision_view_config(s)

    def test_token_budget_too_small_for_thinking_is_rejected(self):
        with pytest.raises(ValidationError):  # below the schema floor
            _settings(v4_decision_view_max_tokens=1024)
        with pytest.raises(V4DecisionViewConfigError, match="too small for thinking"):
            resolve_v4_decision_view_config(_settings(v4_decision_view_max_tokens=2048))

    def test_thinking_disabled_records_no_effort(self):
        cfg = resolve_v4_decision_view_config(_settings(v4_decision_view_thinking="disabled"))
        assert cfg.thinking == "disabled"
        assert cfg.reasoning_effort is None


class TestFactoryWiring:
    def test_factory_builds_deepseek_with_explicit_thinking(self):
        from services.llm.factory import get_llm_provider

        p = get_llm_provider(
            _settings(),
            override_model="deepseek-v4-pro",
            thinking="enabled",
            reasoning_effort="high",
        )
        assert p.model == "deepseek-v4-pro"
        assert p._extra_payload_fields() == {
            "thinking": {"type": "enabled", "reasoning_effort": "high"}
        }

    def test_factory_default_stays_thinking_disabled_for_the_general_model(self):
        """Research jobs and the official V3 engine are unchanged: same model,
        thinking explicitly off."""
        from services.llm.factory import get_llm_provider

        p = get_llm_provider(_settings())
        assert p.model == "deepseek-v4-flash"
        assert p._extra_payload_fields() == {"thinking": {"type": "disabled"}}

    def test_thinking_on_a_non_deepseek_provider_is_refused_up_front(self):
        from services.llm.factory import get_llm_provider

        s = _settings(llm_provider="openai", openai_api_key="k", openai_model="gpt-x")
        with pytest.raises(LLMConfigurationError):
            get_llm_provider(s, thinking="enabled", reasoning_effort="high")


# ---------------------------------------------------------------------------
# The production view generator, with the LLM faked at the factory seam.
# ---------------------------------------------------------------------------


class _FakeLLM:
    def __init__(self, *, model, thinking, reasoning_effort, result=None, error=None):
        self.model = model
        self.thinking = thinking
        self.reasoning_effort = reasoning_effort
        self.calls: list[dict] = []
        self._result = result
        self._error = error

    def generate_structured_result(self, messages, schema, *, temperature=0.0, max_tokens=1024):
        self.calls.append({"schema": schema.__name__, "max_tokens": max_tokens, "n": len(messages)})
        if self._error is not None:
            raise self._error
        return self._result


def _view():
    from schemas.decision import DecisionView

    return DecisionView(
        direction="bullish",
        volatility_view="long_vol",
        rationale="cited rationale",
        bull_case="b",
        bear_case="r",
        key_catalysts="c",
        key_risks="k",
        disclaimer="not investment advice",
    )


@pytest.fixture
def company_and_event(db_session):
    from models.ai_thesis_version import AIThesisVersion
    from models.company import Company
    from models.earnings_calendar_event import EarningsCalendarEvent

    company = Company(ticker="PROV", name="Provenance Co")
    db_session.add(company)
    db_session.flush()
    thesis = AIThesisVersion(
        company_id=company.id,
        business_context="ctx",
        historical_earnings_pattern="hist",
        guidance_trend="g",
        key_risks="k",
        market_setup="m",
        disclaimer="not investment advice",
        citations=[],
        provider="deepseek",
        model="deepseek-v4-flash",
    )
    db_session.add(thesis)
    event = EarningsCalendarEvent(
        symbol="PROV",
        company_name="Provenance Co",
        earnings_date=date(2026, 9, 10),
        earnings_time="AMC",
        source="EARNINGSAPI",
        status="UPCOMING",
    )
    db_session.add(event)
    db_session.flush()
    return company, event


def _generate(db, company, event, fake, settings):
    import services.v4_shadow_orchestration as orch

    captured = {}

    def fake_factory(s, override_provider=None, override_model=None, db=None, *, thinking=None,
                     reasoning_effort=None):
        captured.update(model=override_model, thinking=thinking, effort=reasoning_effort)
        return fake

    with (
        patch("services.llm.factory.get_llm_provider", fake_factory),
        patch("core.config.get_settings", return_value=settings),
    ):
        view = orch.default_view_generator(
            db, company, event, datetime(2026, 9, 10, 19, 30, tzinfo=UTC)
        )
    return view, captured


class TestViewGeneratorProvenance:
    def test_asks_for_the_configured_model_and_records_configured_vs_returned_identity(
        self, db_session, company_and_event
    ):
        company, event = company_and_event
        fake = _FakeLLM(
            model="deepseek-v4-pro",
            thinking="enabled",
            reasoning_effort="high",
            result=(
                _view(),
                GenerateResult(
                    model="deepseek-v4-pro-2026-08",
                    finish_reason="stop",
                    usage=TokenUsage(
                        input_tokens=900,
                        output_tokens=210,
                        reasoning_tokens=1450,
                        cache_hit_tokens=512,
                    ),
                    latency_ms=4321,
                    reasoning_present=True,
                    reasoning_chars=5000,
                ),
            ),
        )
        view, captured = _generate(db_session, company, event, fake, _settings())
        assert captured == {"model": "deepseek-v4-pro", "thinking": "enabled", "effort": "high"}
        assert fake.calls == [{"schema": "DecisionView", "max_tokens": 16384, "n": 2}]
        assert view is not None
        assert view.llm_provider == "deepseek"
        assert view.llm_model == "deepseek-v4-pro"  # configured alias
        assert view.llm_returned_model == "deepseek-v4-pro-2026-08"  # as reported
        assert view.llm_thinking == "enabled"
        assert view.llm_reasoning_effort == "high"
        assert view.llm_max_tokens == 16384
        assert view.llm_finish_reason == "stop"
        assert (view.llm_input_tokens, view.llm_output_tokens) == (900, 210)
        assert view.llm_reasoning_tokens == 1450
        assert view.llm_cache_hit_tokens == 512
        assert view.llm_latency_ms == 4321
        assert view.llm_config_version == V4_DECISION_VIEW_CONFIG_VERSION
        assert view.prompt_version == "decision-view-v1"
        assert view.direction == "bullish" and view.volatility_view == "long_vol"
        # No hidden reasoning anywhere on the frozen view.
        assert not any("chain" in str(v) for v in vars(view).values())

    def test_usage_telemetry_is_model_aware(self, db_session, company_and_event):
        from models.provider_usage_event import ProviderUsageEvent

        company, event = company_and_event
        fake = _FakeLLM(
            model="deepseek-v4-pro", thinking="enabled", reasoning_effort="high",
            result=(
                _view(),
                GenerateResult(
                    model="deepseek-v4-pro",
                    finish_reason="stop",
                    usage=TokenUsage(
                        input_tokens=10, output_tokens=5, reasoning_tokens=99, cache_hit_tokens=3
                    ),
                    latency_ms=12,
                ),
            ),
        )
        _generate(db_session, company, event, fake, _settings())
        row = (
            db_session.query(ProviderUsageEvent)
            .filter_by(operation="v4_decision_view")
            .order_by(ProviderUsageEvent.id.desc())
            .first()
        )
        assert row is not None and row.success is True
        assert (row.model, row.reasoning_effort) == ("deepseek-v4-pro", "high")
        assert (row.input_tokens, row.output_tokens, row.total_tokens) == (10, 5, 15)
        assert (row.reasoning_tokens, row.cache_hit_tokens, row.latency_ms) == (99, 3, 12)

    def test_malformed_output_fails_honestly_without_switching_models(
        self, db_session, company_and_event
    ):
        company, event = company_and_event
        fake = _FakeLLM(
            model="deepseek-v4-pro", thinking="enabled", reasoning_effort="high",
            error=StructuredOutputError("deepseek response did not match schema DecisionView"),
        )
        view, captured = _generate(db_session, company, event, fake, _settings())
        assert view is None
        assert captured["model"] == "deepseek-v4-pro"
        assert len(fake.calls) == 1  # exactly one attempt on the configured model

    def test_missing_configuration_raises_instead_of_using_deepseek_model(
        self, db_session, company_and_event
    ):
        company, event = company_and_event
        fake = _FakeLLM(model="deepseek-v4-flash", thinking="disabled", reasoning_effort=None)
        with pytest.raises(V4DecisionViewConfigError):
            _generate(db_session, company, event, fake, _settings(v4_decision_view_model=None))
        assert fake.calls == []  # nothing was sent anywhere


class TestFrozenDecisionCarriesProvenance:
    def test_generate_shadow_decision_persists_every_provenance_field(
        self, db_session, monkeypatch
    ):
        import test_v4_six_cohort_evidence as cohort

        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.v4_shadow import V4ShadowDecision
        from services.v4_shadow import ShadowDecisionView

        event = EarningsCalendarEvent(
            symbol="SIXC", company_name="Six Cohort Co", earnings_date=date(2026, 9, 10),
            earnings_time="AMC", source="EARNINGSAPI", status="UPCOMING",
        )
        db_session.add(event)
        db_session.flush()
        provenance_view = ShadowDecisionView(
            direction="bullish", volatility_view="long_vol", expected_move_intent=None,
            confidence=None, reasoning="r", evidence_refs={"ai_thesis_version_id": 1},
            llm_provider="deepseek", llm_model="deepseek-v4-pro", prompt_version="decision-view-v1",
            llm_returned_model="deepseek-v4-pro", llm_thinking="enabled",
            llm_reasoning_effort="high", llm_max_tokens=16384, llm_finish_reason="stop",
            llm_input_tokens=1, llm_output_tokens=2, llm_reasoning_tokens=3,
            llm_cache_hit_tokens=4, llm_latency_ms=5,
            llm_config_version=V4_DECISION_VIEW_CONFIG_VERSION,
        )
        monkeypatch.setattr(cohort, "_view", lambda: provenance_view)
        result = cohort._freeze(db_session, event, monkeypatch=monkeypatch)
        row = db_session.get(V4ShadowDecision, result.decision_id)
        assert row is not None
        assert (row.llm_model, row.llm_returned_model) == ("deepseek-v4-pro", "deepseek-v4-pro")
        assert (row.llm_thinking, row.llm_reasoning_effort) == ("enabled", "high")
        assert row.llm_max_tokens == 16384
        assert (row.llm_input_tokens, row.llm_output_tokens, row.llm_reasoning_tokens) == (1, 2, 3)
        assert (row.llm_cache_hit_tokens, row.llm_latency_ms) == (4, 5)
        assert row.llm_finish_reason == "stop"
        assert row.llm_config_version == V4_DECISION_VIEW_CONFIG_VERSION

    def test_a_historical_flash_view_keeps_its_stored_identity(self, db_session, monkeypatch):
        """Rows frozen before this change carry llm_model=deepseek-v4-flash and
        no thinking provenance; nothing rewrites them."""
        import test_v4_six_cohort_evidence as cohort

        from models.earnings_calendar_event import EarningsCalendarEvent
        from models.v4_shadow import V4ShadowDecision

        event = EarningsCalendarEvent(
            symbol="SIXC", company_name="Six Cohort Co", earnings_date=date(2026, 9, 10),
            earnings_time="AMC", source="EARNINGSAPI", status="UPCOMING",
        )
        db_session.add(event)
        db_session.flush()
        result = cohort._freeze(db_session, event, monkeypatch=monkeypatch)  # fixture view: flash
        row = db_session.get(V4ShadowDecision, result.decision_id)
        assert row.llm_model == "deepseek-v4-flash"
        assert row.llm_returned_model is None and row.llm_thinking is None
        # Immutability: the trigger refuses an update that would "upgrade" it.
        from sqlalchemy import text
        from sqlalchemy.exc import InternalError, ProgrammingError

        with pytest.raises((InternalError, ProgrammingError)):
            with db_session.begin_nested():
                db_session.execute(
                    text("update v4_shadow_decision set llm_model='deepseek-v4-pro' where id=:i"),
                    {"i": row.id},
                )


class TestOperationsAndTestConnection:
    def test_operations_describes_the_active_decision_model(self):
        d = describe_v4_decision_view_config(_settings())
        assert d["model"] == "deepseek-v4-pro" and d["thinking"] == "enabled"
        assert d["reasoning_effort"] == "high" and d["config_error"] is None

    def test_test_connection_probes_the_configured_pro_thinking_path(self):
        from models.enums import ProviderHealthStatus
        from services import provider_test_connection as ptc

        seen = {}

        class _Probe:
            model = "deepseek-v4-pro"

            def generate_structured_result(
                self, messages, schema, *, temperature=0.0, max_tokens=1024
            ):
                seen["max_tokens"] = max_tokens
                return schema(ok=True, model_family="deepseek"), GenerateResult(
                    model="deepseek-v4-pro", usage=TokenUsage(input_tokens=1, output_tokens=1,
                                                              reasoning_tokens=7), latency_ms=3
                )

        def fake_factory(s, override_provider=None, override_model=None, db=None, *, thinking=None,
                         reasoning_effort=None):
            seen.update(provider=override_provider, model=override_model, thinking=thinking,
                        effort=reasoning_effort)
            return _Probe()

        with patch.object(ptc, "get_llm_provider", fake_factory):
            status, detail = ptc._test_llm(_settings(), "deepseek", None)
        assert status == ProviderHealthStatus.CONNECTED
        assert seen["model"] == "deepseek-v4-pro" and seen["thinking"] == "enabled"
        assert seen["effort"] == "high" and seen["max_tokens"] == 4096
        assert "reasoning_tokens=7" in (detail or "")

    def test_test_connection_reports_a_configuration_error_instead_of_probing_flash(self):
        from models.enums import ProviderHealthStatus
        from services import provider_test_connection as ptc

        calls = []

        def fake_factory(s, override_provider=None, override_model=None, db=None, **kw):
            calls.append(override_model)
            return SimpleNamespace(model=override_model)

        with patch.object(ptc, "get_llm_provider", fake_factory):
            status, detail = ptc._test_llm(_settings(v4_decision_view_model=None), "deepseek", None)
        assert status == ProviderHealthStatus.UNAVAILABLE
        assert "V4_DECISION_VIEW_MODEL" in (detail or "")
        assert calls == [None]  # only the initial provider construction, no probe request
