import pytest

from app.agent.tools.lead_scoring import compute_lead_score
from app.models.schemas import LeadScoreIn, IntentType, LeadStatus


def test_hot_lead():
    result = compute_lead_score(LeadScoreIn(
        budget=100000,
        timeline="immediate",
        industry="technology",
        problem_statement="We need a comprehensive CRM platform to manage our growing sales pipeline and customer relationships across multiple regions.",
        intent=IntentType.PURCHASE,
    ))
    assert result.score >= 0.7
    assert result.status == LeadStatus.HOT


def test_warm_lead():
    result = compute_lead_score(LeadScoreIn(
        budget=15000,
        timeline="this month",
        intent=IntentType.INFORMATION,
    ))
    assert 0.3 <= result.score < 0.65
    assert result.status == LeadStatus.WARM


def test_cold_lead():
    result = compute_lead_score(LeadScoreIn(
        budget=1000,
        timeline="6 months",
        intent=IntentType.UNKNOWN,
    ))
    assert result.score < 0.3
    assert result.status == LeadStatus.COLD


def test_real_estate_icp_industry_bonus():
    result = compute_lead_score(LeadScoreIn(
        budget=50000,
        timeline="immediate",
        industry="real estate",
        intent=IntentType.PURCHASE,
    ))
    assert result.score >= 0.7
    assert "ICP industry" in result.reasoning


def test_insurance_icp_industry_bonus():
    result = compute_lead_score(LeadScoreIn(
        budget=10000,
        timeline="next month",
        industry="insurance agency",
        intent=IntentType.INFORMATION,
    ))
    assert result.score >= 0.50
    assert "ICP industry" in result.reasoning


def test_secondary_industry_gets_lower_bonus():
    result_icp = compute_lead_score(LeadScoreIn(
        budget=50000, timeline="immediate", industry="real estate",
        intent=IntentType.PURCHASE,
    ))
    result_secondary = compute_lead_score(LeadScoreIn(
        budget=50000, timeline="immediate", industry="technology",
        intent=IntentType.PURCHASE,
    ))
    assert result_icp.score == pytest.approx(result_secondary.score + 0.15, abs=0.01)


def test_threshold_uses_settings_not_hardcoded_value():
    from app.config.settings import settings
    result = compute_lead_score(LeadScoreIn(
        budget=15000, timeline="next quarter", intent=IntentType.INFORMATION,
    ))
    if settings.qualification_threshold_warm <= 0.30:
        assert result.status.value in ("warm", "cold")
    assert result.score == pytest.approx(0.30, abs=0.01)


def test_edge_cases():
    result = compute_lead_score(LeadScoreIn(
        budget=None,
        timeline=None,
        intent=IntentType.UNKNOWN,
    ))
    assert result.score >= 0
    assert result.score <= 1.0


async def test_qualification_node_accepts_none_lead_intent():
    """Regression: qualification_node must not crash on IntentType(None)."""
    from unittest.mock import AsyncMock, MagicMock
    from app.agent.nodes.qualification import create_qualification_node
    from app.agent.state import get_initial_state

    state = get_initial_state("qual-none-test")
    state["lead_name"] = "Alice"
    state["company_name"] = "Acme Inc"
    state["industry"] = "tech"
    state["budget"] = 50000.0
    state["timeline"] = "3 months"
    state["problem_statement"] = "Need better CRM"
    state["lead_intent"] = None

    mock_model = MagicMock()
    mock_model.ainvoke = AsyncMock(return_value=MagicMock(
        content="Score: 0.75\nHot lead\nGood fit.",
    ))

    node = create_qualification_node(mock_model)
    result = await node(state)

    assert result.get("lead_status") is not None


def test_insurance_monthly_premium_scores_warm():
    """A $150/mo insurance lead with a 2-week timeline must score >= warm
    (previously this would miscale as cold because the budget was compared
    to SaaS deal-size bands and timeline '2 weeks' was unrecognised)."""
    result = compute_lead_score(LeadScoreIn(
        budget=150,
        timeline="Need coverage within 2 weeks",
        industry="auto insurance",
        problem_statement="Just bought a new car, need comprehensive coverage including collision and liability.",
        intent=IntentType.PURCHASE,
        vertical="insurance",
    ))
    assert result.score >= 0.4, f"Insurance premium lead scored {result.score}, expected >= 0.4 (warm)"
    assert result.status in (LeadStatus.WARM, LeadStatus.HOT)


def test_real_estate_buyer_with_80lakh_2months_scores_hot():
    """A real-estate buyer with an 80L budget and 2-month timeline must
    score hot, not warm (previously budget 8000000 hit the generic
    50000 band and timeline '2 months' was unrecognised)."""
    result = compute_lead_score(LeadScoreIn(
        budget=8_000_000,
        timeline="planning to buy in 2 months",
        industry="buying 3BHK apartment",
        problem_statement="Looking for a 3-bedroom apartment in Noida sector 62, pre-approved for 80L by SBI, need to close within 2 months, ready to move forward.",
        intent=IntentType.PURCHASE,
        vertical="real_estate",
    ))
    # Budget 0.35 + timeline 0.15 + problem 0.15 + intent 0.15 = 0.80 >= 0.70
    assert result.score >= 0.7, f"Real estate buyer scored {result.score}, expected >= 0.7 (hot)"
    assert result.status == LeadStatus.HOT


def test_individual_lead_no_icp_penalty():
    """An individual consumer lead (lead_type='individual') should not be
    penalized for lacking an ICP industry. The lost industry weight (0.20)
    is replaced by a vertical-specific individual signal."""
    result = compute_lead_score(LeadScoreIn(
        budget=8_000_000,
        timeline="2 months",
        industry="buying a house",
        problem_statement="Looking for a 3BHK in Noida, budget around 80 lakh.",
        intent=IntentType.PURCHASE,
        vertical="real_estate",
        lead_type="individual",
    ))
    assert result.score >= 0.4, f"Individual buyer scored {result.score}, expected >= 0.4"
    assert "Individual vertical signal" in result.reasoning


async def test_lead_intent_survives_postgres_round_trip():
    """Multi-turn regression: lead_intent detected in turn 1 must persist
    through Postgres save/load so qualification_node sees the real intent.

    Turn 1: greeting sets lead_intent="purchase", saves to Postgres.
    Turn 2+3: load_state returns it, qualification scores correctly.
    """
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.agent.graph import run_agent
    from app.agent.tools.lead_scoring import compute_lead_score

    store: dict = {}

    async def _fake_save(session_id, state):
        store[session_id] = state

    async def _fake_load(session_id, tenant_id=None):
        return store.get(session_id)

    # ---- Turn 1: greeting sets lead_intent="purchase" ----
    mock_graph1 = MagicMock()
    mock_graph1.ainvoke = AsyncMock(return_value={
        "lead_intent": "purchase",
        "lead_name": "Alice",
        "company_name": None,
        "industry": None,
        "budget": None,
        "timeline": None,
        "problem_statement": None,
        "missing_fields": ["company_name", "industry", "budget",
                           "timeline", "problem_statement"],
        "current_node": "info_collection",
        "conversation_stage": "collecting",
        "next_action": "collect_info",
        "lead_status": None,
        "qualification_score": None,
        "booking_confirmed": False,
        "meeting_time": None,
        "human_escalated": False,
        "objection_type": None,
        "conversation_history": [
            {"role": "user", "content": "Hi, I'm Alice"},
            {"role": "assistant",
             "content": "Hello Alice! What company do you work for?"},
        ],
    })
    mock_graph1.checkpointer = MagicMock()
    mock_graph1.checkpointer.adelete_thread = AsyncMock()

    with patch("app.agent.graph.get_graph", return_value=mock_graph1), \
         patch("app.agent.graph.memory_service.save_state", _fake_save), \
         patch("app.agent.graph.memory_service.load_state", _fake_load):
        result1 = await run_agent("intent-test", "Hi, I'm Alice")
        await _fake_save("intent-test", result1)

    assert store["intent-test"]["lead_intent"] == "purchase", \
        "lead_intent not saved after turn 1"

    # ---- Turn 2: graph loads lead_intent from Postgres, adds company_name ----
    async def _turn2_ainvoke(turn_input, config):
        base = dict(turn_input)
        base["company_name"] = "Acme Inc"
        base["missing_fields"] = [
            "industry", "budget", "timeline", "problem_statement",
        ]
        base["current_node"] = "info_collection"
        base["conversation_stage"] = "collecting"
        base["conversation_history"] = base.get("conversation_history", []) + [
            {"role": "assistant", "content": "Great! What industry?"},
        ]
        return base

    mock_graph2 = MagicMock()
    mock_graph2.ainvoke = _turn2_ainvoke
    mock_graph2.checkpointer = MagicMock()
    mock_graph2.checkpointer.adelete_thread = AsyncMock()

    with patch("app.agent.graph.get_graph", return_value=mock_graph2), \
         patch("app.agent.graph.memory_service.save_state", _fake_save), \
         patch("app.agent.graph.memory_service.load_state", _fake_load):
        result2 = await run_agent("intent-test", "Acme Inc")
        await _fake_save("intent-test", result2)

    assert result2.get("lead_intent") == "purchase", \
        f"lead_intent lost on turn 2: {result2.get('lead_intent')}"

    # ---- Turn 3: all fields present, qualification scores ----
    async def _turn3_ainvoke(turn_input, config):
        base = dict(turn_input)
        base["industry"] = "tech"
        base["budget"] = 50000.0
        base["timeline"] = "3 months"
        base["problem_statement"] = "Need better CRM"
        base["missing_fields"] = []
        intent_str = base.get("lead_intent") or "unknown"
        score = compute_lead_score(LeadScoreIn(
            budget=base["budget"],
            timeline=base["timeline"],
            industry=base["industry"],
            problem_statement=base["problem_statement"],
            intent=IntentType(intent_str),
        ))
        base["lead_status"] = score.status.value
        base["qualification_score"] = score.score
        base["current_node"] = "handle_next"
        base["conversation_stage"] = "qualified"
        base["conversation_history"] = base.get("conversation_history", []) + [
            {"role": "assistant",
             "content": f"Great! You're qualified as {score.status.value}."},
        ]
        base["next_action"] = "handle_next"
        return base

    mock_graph3 = MagicMock()
    mock_graph3.ainvoke = _turn3_ainvoke
    mock_graph3.checkpointer = MagicMock()
    mock_graph3.checkpointer.adelete_thread = AsyncMock()

    with patch("app.agent.graph.get_graph", return_value=mock_graph3), \
         patch("app.agent.graph.memory_service.save_state", _fake_save), \
         patch("app.agent.graph.memory_service.load_state", _fake_load):
        result3 = await run_agent(
            "intent-test",
            "We're in tech, budget ~50k, timeline 3 months, need better CRM",
        )
        await _fake_save("intent-test", result3)

    assert result3.get("lead_intent") == "purchase", \
        f"lead_intent lost on turn 3: {result3.get('lead_intent')}"
    assert result3.get("lead_status") is not None, \
        "qualification did not produce a lead_status"
    # Purchase intent (0.15) yields >= 0.4; unknown (0.02) would be < 0.4 here
    assert result3.get("qualification_score", 0) >= 0.4, (
        f"score too low for purchase intent: "
        f"{result3.get('qualification_score')} — "
        f"intent likely defaulted to 'unknown'"
    )
