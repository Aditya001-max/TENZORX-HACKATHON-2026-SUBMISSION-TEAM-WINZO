
from __future__ import annotations
import hashlib
from typing import Dict, Any, List, Tuple


# ---------------- mock bureau ----------------

def fetch_bureau_score(pan: str | None, phone: str | None) -> Dict[str, Any]:

    seed = (pan or "") + (phone or "") + "salt"
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    score = 600 + (h % 301)             # 600..900

    # Skewed delinquency distribution
    d_roll = h % 100
    if d_roll < 65:
        delinquencies = 0
    elif d_roll < 90:
        delinquencies = 1
    else:
        delinquencies = 2 + (h % 2)     # 2 or 3

    open_loans = (h >> 8) % 4           # 0..3
    return {
        "bureau_score": score,
        "delinquencies_24m": delinquencies,
        "open_loans": open_loans,
        "source": "mock_bureau_v1",
    }


# ---------------- policy ----------------

POLICY = {
    "min_age": 21,
    "max_age": 60,
    "min_income": 20000,                # INR/month
    "min_bureau_score": 650,
    "max_age_diff": 8,                  # |declared - estimated|
    "max_foir": 0.55,                   # fixed-obligations-to-income ratio
    "max_loan_multiplier": 24,          # loan <= 24x monthly income
    "min_loan": 50000,
    "max_loan": 2500000,
    "tenures_months": [12, 24, 36, 48, 60],
    "base_rate": 13.5,                  # %
}


def evaluate_policy(extracted: Dict[str, Any],
                    estimated_age: int | None,
                    bureau: Dict[str, Any]) -> Tuple[List[str], List[str]]:
    """
    Returns (hard_failures, soft_warnings).
    Hard failures => decision = REJECT.
    Soft warnings => decision = REFER (manual review) or reduced limit.
    """
    hard, soft = [], []

    age = extracted.get("declared_age")
    if not age:
        soft.append("AGE_NOT_DECLARED")
    else:
        if age < POLICY["min_age"]:
            hard.append(f"AGE_BELOW_MIN ({age} < {POLICY['min_age']})")
        if age > POLICY["max_age"]:
            hard.append(f"AGE_ABOVE_MAX ({age} > {POLICY['max_age']})")

    if estimated_age and age:
        if abs(estimated_age - age) > POLICY["max_age_diff"]:
            soft.append(f"AGE_MISMATCH (declared={age}, estimated={estimated_age})")

    income = extracted.get("monthly_income") or 0
    if income < POLICY["min_income"]:
        hard.append(f"INCOME_BELOW_MIN ({int(income)} < {POLICY['min_income']})")

    emp = extracted.get("employment_type")
    if emp == "unemployed":
        hard.append("EMPLOYMENT_UNEMPLOYED")
    if emp is None:
        soft.append("EMPLOYMENT_NOT_DECLARED")

    score = bureau.get("bureau_score", 0)
    if score < POLICY["min_bureau_score"]:
        if score < 600:
            hard.append(f"BUREAU_SCORE_LOW ({score})")
        else:
            soft.append(f"BUREAU_SCORE_BORDERLINE ({score})")

    if bureau.get("delinquencies_24m", 0) >= 2:
        hard.append(f"DELINQUENCIES_HIGH ({bureau['delinquencies_24m']})")
    elif bureau.get("delinquencies_24m", 0) == 1:
        soft.append("DELINQUENCY_PRESENT")

    return hard, soft


# ---------------- scoring ----------------

def compute_risk_score(extracted: Dict[str, Any], bureau: Dict[str, Any]) -> int:
    """
    Lower = riskier. Scale 0-100.
    A simple weighted composite acting as a stand-in for an XGBoost model.
    """
    score = 50

    bs = bureau.get("bureau_score", 650)
    score += int((bs - 650) / 5)                              # +/- ~50

    income = extracted.get("monthly_income") or 0
    if income >= 80000:
        score += 15
    elif income >= 40000:
        score += 8
    elif income >= 25000:
        score += 3

    emp = extracted.get("employment_type")
    score += {"salaried": 10, "professional": 12, "self_employed": 4,
              "retired": 0, "unemployed": -25}.get(emp, 0)

    score -= bureau.get("delinquencies_24m", 0) * 8
    score -= bureau.get("open_loans", 0) * 2

    return max(0, min(100, score))


def compute_propensity(extracted: Dict[str, Any]) -> int:
    """
    Likelihood of acceptance / repayment intent. 0-100.
    """
    p = 50
    purpose = extracted.get("loan_purpose")
    p += {"home_renovation": 12, "education": 15, "medical": 10,
          "wedding": 8, "business": 6, "vehicle": 5,
          "debt_consolidation": -3, "travel": -5}.get(purpose, 0)

    if extracted.get("pan"):
        p += 10
    if extracted.get("employer_name"):
        p += 5
    return max(0, min(100, p))


def risk_band(score: int) -> str:
    if score >= 75:
        return "LOW"
    if score >= 55:
        return "MEDIUM"
    if score >= 40:
        return "HIGH"
    return "VERY_HIGH"


# ---------------- offer generation ----------------

def _emi(principal: float, annual_rate: float, months: int) -> float:
    r = (annual_rate / 100) / 12
    if r == 0:
        return principal / months
    return principal * r * (1 + r) ** months / ((1 + r) ** months - 1)


def _interest_for_band(band: str) -> float:
    return {
        "LOW":      POLICY["base_rate"],
        "MEDIUM":   POLICY["base_rate"] + 2.5,
        "HIGH":     POLICY["base_rate"] + 5.0,
        "VERY_HIGH": POLICY["base_rate"] + 8.0,
    }[band]


def _max_eligible_loan(income: float, existing_emi: float, rate: float, months: int) -> float:
    """Loan amount such that total EMI <= max_foir * income."""
    available_emi = max(0, POLICY["max_foir"] * income - (existing_emi or 0))
    if available_emi <= 0:
        return 0
    r = (rate / 100) / 12
    if r == 0:
        return available_emi * months
    # invert the EMI formula
    return available_emi * ((1 + r) ** months - 1) / (r * (1 + r) ** months)


def generate_offer(extracted: Dict[str, Any], band: str,
                   requested_amount: float | None = None) -> Dict[str, Any]:
    income = extracted.get("monthly_income") or 0
    existing_emi = extracted.get("existing_emi") or 0
    rate = _interest_for_band(band)

    # cap by both FOIR and income multiplier
    options = []
    for tenure in POLICY["tenures_months"]:
        cap_foir = _max_eligible_loan(income, existing_emi, rate, tenure)
        cap_mult = income * POLICY["max_loan_multiplier"]
        eligible = min(cap_foir, cap_mult, POLICY["max_loan"])

        if requested_amount:
            principal = min(eligible, requested_amount)
        else:
            principal = eligible

        # round to nearest 10,000
        principal = max(POLICY["min_loan"], int(principal // 10000) * 10000)
        if principal < POLICY["min_loan"]:
            continue

        emi = round(_emi(principal, rate, tenure), 2)
        total = round(emi * tenure, 2)
        options.append({
            "tenure_months": tenure,
            "loan_amount": principal,
            "interest_rate": rate,
            "emi": emi,
            "total_payable": total,
            "total_interest": round(total - principal, 2),
        })

    if not options:
        return {"eligible": False, "reason": "Insufficient income for any tenure"}

    # primary recommendation: best balance — middle tenure
    primary = options[len(options) // 2]
    return {
        "eligible": True,
        "primary_offer": primary,
        "all_offers": options,
        "currency": "INR",
        "interest_rate": rate,
        "loan_amount": primary["loan_amount"],
        "tenure_months": primary["tenure_months"],
        "emi": primary["emi"],
    }


# ---------------- top-level ----------------

def assess(extracted: Dict[str, Any], estimated_age: int | None) -> Dict[str, Any]:
    bureau = fetch_bureau_score(extracted.get("pan"), extracted.get("phone"))
    hard, soft = evaluate_policy(extracted, estimated_age, bureau)

    risk = compute_risk_score(extracted, bureau)
    propensity = compute_propensity(extracted)
    band = risk_band(risk)

    if hard:
        decision = "REJECT"
        offer = {"eligible": False, "reason": "Policy failure", "failures": hard}
    elif soft:
        decision = "REFER"
        offer = generate_offer(extracted, band,
                               requested_amount=extracted.get("loan_amount_requested"))
    else:
        decision = "APPROVE"
        offer = generate_offer(extracted, band,
                               requested_amount=extracted.get("loan_amount_requested"))

    return {
        "decision": decision,
        "risk_score": risk,
        "propensity_score": propensity,
        "risk_band": band,
        "bureau_score": bureau["bureau_score"],
        "bureau": bureau,
        "policy_failures": hard,
        "policy_warnings": soft,
        "reasons": hard + soft,
        "offer": offer,
    }
