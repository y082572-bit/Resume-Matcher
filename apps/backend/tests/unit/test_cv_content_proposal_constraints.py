"""``cv_content_proposal_constraints``: deterministic extraction + validation.

All source text is synthetic placeholder content -- no real person or CV
data is used.
"""

from __future__ import annotations

from uuid import uuid4

from app.schemas.cv_content_proposal import ImmutableConstraintType, ProposalViolationCode
from app.services.cv_content_proposal_constraints import (
    build_immutable_constraint_set,
    check_immutable_constraints,
    compute_immutable_constraint_set_fingerprint,
    extract_structured_identity_constraints,
    extract_text_constraints,
)


def _types(constraints) -> set[ImmutableConstraintType]:
    return {c.constraint_type for c in constraints}


def test_extracts_integer() -> None:
    constraints = extract_text_constraints("Led a team of 12 engineers.")
    values = [c.normalized_value for c in constraints if c.constraint_type == ImmutableConstraintType.INTEGER]
    assert "12" in values


def test_extracts_decimal() -> None:
    constraints = extract_text_constraints("Improved uptime to 99.95 reliability score.")
    values = [c.normalized_value for c in constraints if c.constraint_type == ImmutableConstraintType.DECIMAL]
    assert "99.95" in values


def test_extracts_percentage() -> None:
    constraints = extract_text_constraints("Grew revenue by 30% year over year.")
    assert ImmutableConstraintType.PERCENTAGE in _types(constraints)
    values = [c.normalized_value for c in constraints if c.constraint_type == ImmutableConstraintType.PERCENTAGE]
    assert values == ["30%"]


def test_extracts_currency() -> None:
    constraints = extract_text_constraints("Managed a budget of 1000 PLN monthly.")
    values = [c.normalized_value for c in constraints if c.constraint_type == ImmutableConstraintType.CURRENCY_AMOUNT]
    assert values == ["1000pln"]


def test_extracts_currency_symbol_amount() -> None:
    constraints = extract_text_constraints("Closed a deal worth $500 for the client.")
    values = [c.normalized_value for c in constraints if c.constraint_type == ImmutableConstraintType.CURRENCY_AMOUNT]
    assert values == ["500$"]


def test_extracts_numeric_range() -> None:
    constraints = extract_text_constraints("Supervised teams of 10-15 people.")
    values = [c.normalized_value for c in constraints if c.constraint_type == ImmutableConstraintType.NUMERIC_RANGE]
    assert values == ["10-15"]


def test_extracts_date() -> None:
    constraints = extract_text_constraints("Started the initiative on 2020-01-01.")
    values = [c.normalized_value for c in constraints if c.constraint_type == ImmutableConstraintType.DATE]
    assert "2020-01-01" in values


def test_extracts_period() -> None:
    constraints = extract_text_constraints("Worked there from 2019-2021 continuously.")
    values = [c.normalized_value for c in constraints if c.constraint_type == ImmutableConstraintType.PERIOD]
    assert values == ["2019~2021"]


def test_extracts_inequality() -> None:
    constraints = extract_text_constraints("Delivered results in >90% of cases.")
    ineq_values = [c.normalized_value for c in constraints if c.constraint_type == ImmutableConstraintType.INEQUALITY]
    assert ineq_values == [">"]


def test_extracts_unit_attached_to_number() -> None:
    constraints = extract_text_constraints("Trained 10 people over 5 years.")
    unit_values = sorted(
        c.normalized_value for c in constraints if c.constraint_type == ImmutableConstraintType.UNIT
    )
    assert unit_values == ["people", "years"]


def test_extracts_company_and_role_from_structured_fields() -> None:
    constraints = extract_structured_identity_constraints(
        "EMPLOYMENT_ROLE", {"firma": "Synthetic Co", "stanowisko": "Synthetic Analyst"}
    )
    by_type = {c.constraint_type: c.raw_text for c in constraints}
    assert by_type[ImmutableConstraintType.COMPANY_NAME] == "Synthetic Co"
    assert by_type[ImmutableConstraintType.ROLE_NAME] == "Synthetic Analyst"


def test_extracts_negation() -> None:
    constraints = extract_text_constraints("Delivered the project without any delays.")
    assert ImmutableConstraintType.NEGATION in _types(constraints)


# -- missing/changed/added token validation --


def _constraint_set(text: str, fact_type: str = "ACHIEVEMENT"):
    return build_immutable_constraint_set(fact_id=uuid4(), fact_type=fact_type, source_text=text)


def test_missing_token_blocked() -> None:
    source = _constraint_set("Grew revenue by 30% in one year.")
    result = check_immutable_constraints(source, "Grew revenue significantly in one year.")
    assert result is not None
    code, _ = result
    assert code == ProposalViolationCode.IMMUTABLE_TOKEN_MISSING


def test_changed_token_blocked() -> None:
    source = _constraint_set("Grew revenue by 30% in one year.")
    result = check_immutable_constraints(source, "Grew revenue by 45% in one year.")
    assert result is not None
    code, _ = result
    assert code == ProposalViolationCode.IMMUTABLE_TOKEN_CHANGED


def test_new_numeric_token_blocked() -> None:
    source = _constraint_set("Grew revenue significantly in one year.")
    result = check_immutable_constraints(source, "Grew revenue by 30% in one year.")
    assert result is not None
    code, _ = result
    assert code == ProposalViolationCode.IMMUTABLE_TOKEN_ADDED


def test_additional_percentage_blocked() -> None:
    source = _constraint_set("Grew revenue by 30%.")
    result = check_immutable_constraints(source, "Grew revenue by 30% and cut costs by 10%.")
    assert result is not None
    code, _ = result
    assert code == ProposalViolationCode.IMMUTABLE_TOKEN_ADDED


def test_changed_currency_blocked() -> None:
    source = _constraint_set("Managed a budget of 1000 PLN.")
    result = check_immutable_constraints(source, "Managed a budget of 1000 USD.")
    assert result is not None
    code, _ = result
    assert code == ProposalViolationCode.IMMUTABLE_TOKEN_CHANGED


def test_changed_date_blocked() -> None:
    source = _constraint_set("Joined on 2020-01-01.")
    result = check_immutable_constraints(source, "Joined on 2021-01-01.")
    assert result is not None
    code, _ = result
    assert code == ProposalViolationCode.IMMUTABLE_TOKEN_CHANGED


def test_changed_company_blocked() -> None:
    source = build_immutable_constraint_set(
        fact_id=uuid4(),
        fact_type="EMPLOYMENT_ROLE",
        source_text="Synthetic Analyst, Synthetic Co",
        structured_fields={"firma": "Synthetic Co", "stanowisko": "Synthetic Analyst"},
    )
    result = check_immutable_constraints(source, "Synthetic Analyst, Other Co")
    assert result is not None
    code, _ = result
    assert code == ProposalViolationCode.IMMUTABLE_TOKEN_CHANGED


def test_changed_role_blocked() -> None:
    source = build_immutable_constraint_set(
        fact_id=uuid4(),
        fact_type="EMPLOYMENT_ROLE",
        source_text="Synthetic Analyst, Synthetic Co",
        structured_fields={"firma": "Synthetic Co", "stanowisko": "Synthetic Analyst"},
    )
    result = check_immutable_constraints(source, "Synthetic Manager, Synthetic Co")
    assert result is not None
    code, _ = result
    assert code == ProposalViolationCode.IMMUTABLE_TOKEN_CHANGED


def test_removed_negation_blocked() -> None:
    source = _constraint_set("Delivered the project without any delays.")
    result = check_immutable_constraints(source, "Delivered the project with some delays.")
    assert result is not None
    code, _ = result
    assert code == ProposalViolationCode.IMMUTABLE_TOKEN_MISSING


def test_added_negation_blocked() -> None:
    source = _constraint_set("Delivered the project with some delays.")
    result = check_immutable_constraints(source, "Delivered the project without any delays.")
    assert result is not None
    code, _ = result
    assert code == ProposalViolationCode.IMMUTABLE_TOKEN_ADDED


def test_rephrased_text_preserving_all_tokens_passes() -> None:
    source = _constraint_set("Grew revenue by 30% in one year.")
    result = check_immutable_constraints(source, "Achieved a 30% revenue increase within one year.")
    assert result is None


# -- deterministic fingerprints --


def test_constraint_set_fingerprint_is_deterministic() -> None:
    fact_id = uuid4()
    first = build_immutable_constraint_set(fact_id=fact_id, fact_type="ACHIEVEMENT", source_text="Grew revenue by 30%.")
    second = build_immutable_constraint_set(fact_id=fact_id, fact_type="ACHIEVEMENT", source_text="Grew revenue by 30%.")
    assert first.immutable_constraint_set_fingerprint == second.immutable_constraint_set_fingerprint
    assert first.immutable_constraint_set_fingerprint == compute_immutable_constraint_set_fingerprint(
        fact_id=fact_id, constraints=first.constraints
    )


def test_constraint_set_fingerprint_changes_with_text() -> None:
    fact_id = uuid4()
    first = build_immutable_constraint_set(fact_id=fact_id, fact_type="ACHIEVEMENT", source_text="Grew revenue by 30%.")
    second = build_immutable_constraint_set(fact_id=fact_id, fact_type="ACHIEVEMENT", source_text="Grew revenue by 40%.")
    assert first.immutable_constraint_set_fingerprint != second.immutable_constraint_set_fingerprint


# -- decimal-comma / decimal-point canonicalization (percentage) --


def _percentage_values(text: str) -> list[str]:
    constraints = extract_text_constraints(text)
    return [
        c.normalized_value for c in constraints if c.constraint_type == ImmutableConstraintType.PERCENTAGE
    ]


def test_extract_percentage_with_comma_preserves_value() -> None:
    values = _percentage_values("Zmniejszono churn o 12,99% w tym kwartale.")
    assert values == ["12.99%"]


def test_extract_percentage_with_dot_preserves_value() -> None:
    values = _percentage_values("Reduced churn by 12.99% this quarter.")
    assert values == ["12.99%"]


def test_percentage_comma_vs_dot_notation_passes() -> None:
    source = _constraint_set("Zmniejszono churn o 12,99% w tym kwartale.")
    result = check_immutable_constraints(source, "Zmniejszono churn o 12.99% w tym kwartale.")
    assert result is None


def test_percentage_comma_to_different_comma_value_blocked() -> None:
    source = _constraint_set("Zmniejszono churn o 12,99% w tym kwartale.")
    result = check_immutable_constraints(source, "Zmniejszono churn o 1,299% w tym kwartale.")
    assert result is not None
    code, _ = result
    assert code == ProposalViolationCode.IMMUTABLE_TOKEN_CHANGED


def test_percentage_comma_to_concatenated_digits_blocked() -> None:
    source = _constraint_set("Zmniejszono churn o 12,99% w tym kwartale.")
    result = check_immutable_constraints(source, "Zmniejszono churn o 1299% w tym kwartale.")
    assert result is not None
    code, _ = result
    assert code == ProposalViolationCode.IMMUTABLE_TOKEN_CHANGED


def test_percentage_comma_to_nearby_comma_value_blocked() -> None:
    source = _constraint_set("Zmniejszono churn o 12,99% w tym kwartale.")
    result = check_immutable_constraints(source, "Zmniejszono churn o 12,98% w tym kwartale.")
    assert result is not None
    code, _ = result
    assert code == ProposalViolationCode.IMMUTABLE_TOKEN_CHANGED


# -- amount multiplier + currency extraction --


def _currency_values(text: str) -> list[str]:
    constraints = extract_text_constraints(text)
    return [
        c.normalized_value for c in constraints if c.constraint_type == ImmutableConstraintType.CURRENCY_AMOUNT
    ]


def test_extract_amount_with_multiplier_preserves_value_multiplier_and_currency() -> None:
    values = _currency_values("Wynegocjowano budżet 20 mln zł na projekt.")
    assert values == ["20|mln|zl"]


def test_amount_multiplier_currency_changed_to_other_currency_blocked() -> None:
    source = _constraint_set("Wynegocjowano budżet 20 mln zł na projekt.")
    result = check_immutable_constraints(source, "Wynegocjowano budżet 20 mln USD na projekt.")
    assert result is not None
    code, _ = result
    assert code == ProposalViolationCode.IMMUTABLE_TOKEN_CHANGED


def test_amount_multiplier_currency_changed_to_other_multiplier_blocked() -> None:
    source = _constraint_set("Wynegocjowano budżet 20 mln zł na projekt.")
    result = check_immutable_constraints(source, "Wynegocjowano budżet 20 mld zł na projekt.")
    assert result is not None
    code, _ = result
    assert code == ProposalViolationCode.IMMUTABLE_TOKEN_CHANGED


def test_amount_multiplier_currency_changed_amount_blocked() -> None:
    source = _constraint_set("Wynegocjowano budżet 20 mln zł na projekt.")
    result = check_immutable_constraints(source, "Wynegocjowano budżet 21 mln zł na projekt.")
    assert result is not None
    code, _ = result
    assert code == ProposalViolationCode.IMMUTABLE_TOKEN_CHANGED


def test_amount_multiplier_currency_dropped_currency_blocked() -> None:
    source = _constraint_set("Wynegocjowano budżet 20 mln zł na projekt.")
    result = check_immutable_constraints(source, "Wynegocjowano budżet 20 mln na projekt.")
    assert result is not None


def test_amount_multiplier_currency_unchanged_passes() -> None:
    source = _constraint_set("Wynegocjowano budżet 20 mln zł na projekt.")
    result = check_immutable_constraints(source, "Wynegocjowano budżet 20 mln zł na projekt.")
    assert result is None


def test_extract_amount_with_dotted_thousands_multiplier() -> None:
    values = _currency_values("Zainwestowano 20 tys. PLN w kampanię.")
    assert values == ["20|tys|pln"]


def test_extract_amount_with_billions_multiplier() -> None:
    values = _currency_values("Fundusz zarządzał kwotą 5 mld EUR.")
    assert values == ["5|mld|eur"]
