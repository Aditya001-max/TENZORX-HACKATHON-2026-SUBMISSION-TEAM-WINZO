

import re
from typing import Dict, Any, Optional


# ---------------- helpers ----------------

NUMBER_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4,
    "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
    "ten": 10, "eleven": 11, "twelve": 12, "thirteen": 13,
    "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30,
    "forty": 40, "fifty": 50, "sixty": 60, "seventy": 70,
    "eighty": 80, "ninety": 90, "hundred": 100, "thousand": 1000,
    "lakh": 100000, "lac": 100000, "crore": 10000000,
}


def _words_to_number(text: str) -> Optional[float]:
    """Very lightweight word-to-number conversion for INR amounts/ages."""
    text = text.lower()
    tokens = re.findall(r"[a-z]+|\d+\.?\d*", text)
    total = 0
    current = 0
    found = False
    for tok in tokens:
        if tok.replace(".", "", 1).isdigit():
            current += float(tok)
            found = True
            continue
        if tok in NUMBER_WORDS:
            val = NUMBER_WORDS[tok]
            found = True
            if val == 100:
                current = (current or 1) * 100
            elif val in (1000, 100000, 10000000):
                current = (current or 1) * val
                total += current
                current = 0
            else:
                current += val
    total += current
    return total if found else None


def _extract_amount(text: str) -> Optional[float]:
    """
    Extract an INR amount. Handles:
      - 'fifty thousand', '2 lakh', '2.5 lakhs', '50000', '₹1,20,000'
    """
    t = text.lower().replace(",", "").replace("rs.", "").replace("rs", "").replace("₹", "")

    # Pattern: '2 lakh', '2.5 lakh', '50 thousand'
    m = re.search(r"(\d+\.?\d*)\s*(lakh|lac|lakhs|crore|crores|thousand|k)\b", t)
    if m:
        val = float(m.group(1))
        unit = m.group(2)
        mult = {"lakh": 1e5, "lac": 1e5, "lakhs": 1e5,
                "crore": 1e7, "crores": 1e7,
                "thousand": 1e3, "k": 1e3}[unit]
        return val * mult

    # Plain digits >= 1000
    m2 = re.search(r"\b(\d{4,9})\b", t)
    if m2:
        return float(m2.group(1))

    # Words like 'fifty thousand'
    wn = _words_to_number(t)
    if wn and wn >= 1000:
        return wn
    return None


def _extract_age(text: str) -> Optional[int]:
    t = text.lower()
    m = re.search(r"(\d{2})\s*(?:years|yrs|year)?\s*(?:old)?", t)
    if m:
        a = int(m.group(1))
        if 18 <= a <= 80:
            return a
    # word form like 'thirty two'
    wn = _words_to_number(t)
    if wn and 18 <= wn <= 80:
        return int(wn)
    return None


# ---------------- field extractors ----------------

EMPLOYMENT_KEYWORDS = {
    "salaried": ["salaried", "employee", "job", "company", "employed at", "work at",
                 "working at", "i work for", "private job", "government job", "psu"],
    "self_employed": ["self employed", "self-employed", "business", "businessman",
                      "shop owner", "freelancer", "consultant", "own business",
                      "entrepreneur", "trader"],
    "professional": ["doctor", "lawyer", "ca", "chartered accountant", "architect",
                     "professional"],
    "retired": ["retired", "pensioner", "pension"],
    "unemployed": ["unemployed", "not working", "no job", "jobless"],
}

LOAN_PURPOSES = {
    "home_renovation": ["renovation", "renovate", "home improvement", "house repair"],
    "wedding": ["wedding", "marriage", "shaadi"],
    "education": ["education", "study", "studies", "college", "tuition", "course"],
    "medical": ["medical", "treatment", "hospital", "surgery", "health"],
    "vehicle": ["car", "bike", "vehicle", "scooter", "two wheeler"],
    "business": ["business", "expand business", "working capital", "shop"],
    "travel": ["travel", "vacation", "trip", "holiday"],
    "debt_consolidation": ["consolidate", "pay off", "existing loan", "credit card"],
    "personal": ["personal", "household", "family expense"],
}


def detect_employment(text: str) -> Optional[str]:
    t = text.lower()
    for category, keywords in EMPLOYMENT_KEYWORDS.items():
        if any(k in t for k in keywords):
            return category
    return None


def detect_loan_purpose(text: str) -> Optional[str]:
    t = text.lower()
    for purpose, keywords in LOAN_PURPOSES.items():
        if any(k in t for k in keywords):
            return purpose
    return None


def detect_consent(text: str) -> bool:
    t = text.lower().strip()
    affirmatives = ["yes", "i agree", "i consent", "i give my consent", "agreed",
                    "i do", "absolutely", "of course", "sure", "i accept",
                    "haan", "ji haan", "haan ji", "i am okay", "okay i agree"]
    return any(a in t for a in affirmatives) and not any(
        n in t for n in ["no", "not", "don't", "do not", "decline", "refuse"]
    )


def detect_employer(text: str) -> Optional[str]:
    """Try to capture an employer name after 'work at|for|with'."""
    m = re.search(r"(?:work(?:ing)?\s+(?:at|for|with|in)|employed\s+at|company\s+is)\s+([A-Z][A-Za-z0-9 &\.]{1,40})",
                  text)
    if m:
        return m.group(1).strip().rstrip(".,")
    return None


def detect_pan(text: str) -> Optional[str]:
    """PAN format: AAAAA9999A (5 letters, 4 digits, 1 letter)."""
    # Match either with normal word boundary or in a contiguous block
    upper = text.upper()
    m = re.search(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b", upper)
    if m:
        return m.group(1)
    # Allow PAN spelled with a space here and there
    no_space = re.sub(r"\s+", "", upper)
    m2 = re.search(r"([A-Z]{5}[0-9]{4}[A-Z])", no_space)
    return m2.group(1) if m2 else None


def detect_name(text: str) -> Optional[str]:
    """Capture name after common phrasings, stopping at sentence/clause boundaries."""
    # Stop tokens that mark the end of a name in spoken sentences
    stop = r"(?:\s+(?:and|i\s+am|i'm|aged|age|from|of|here|in|at|with|sir|madam)\b|[,.!?]|$)"
    patterns = [
        rf"my name is\s+([A-Za-z][A-Za-z ]{{1,40}}?){stop}",
        rf"this is\s+([A-Za-z][A-Za-z ]{{1,40}}?){stop}",
        rf"^i\s+am\s+([A-Z][A-Za-z]+(?:\s+[A-Z][A-Za-z]+){{0,3}})",
        rf"^([A-Z][a-z]+(?:\s+[A-Z][a-z]+){{1,3}})\s*(?:here|speaking)",
    ]
    for p in patterns:
        m = re.search(p, text, re.IGNORECASE | re.MULTILINE)
        if m:
            name = m.group(1).strip().rstrip(".,")
            if name.lower() not in {"here", "good", "fine", "okay", "ready"}:
                return " ".join(w.capitalize() for w in name.split())
    return None


# ---------------- top-level orchestrator ----------------

def extract_all(text: str, prompt_context: Optional[str] = None) -> Dict[str, Any]:
    """
    Extract every possible field from a single utterance.
    `prompt_context` (e.g. 'income', 'employment', 'consent') biases extraction
    toward the field the agent just asked about — this is how the agentic
    flow keeps things accurate.
    """
    out: Dict[str, Any] = {}

    name = detect_name(text)
    if name:
        out["full_name"] = name

    age = _extract_age(text)
    if age and (prompt_context in (None, "age", "intro")):
        out["declared_age"] = age

    emp = detect_employment(text)
    if emp:
        out["employment_type"] = emp

    employer = detect_employer(text)
    if employer:
        out["employer_name"] = employer

    pan = detect_pan(text)
    if pan:
        out["pan"] = pan

    purpose = detect_loan_purpose(text)
    if purpose:
        out["loan_purpose"] = purpose

    amount = _extract_amount(text)
    if amount:
        # context decides whether amount is income or loan request
        if prompt_context == "income":
            out["monthly_income"] = amount
        elif prompt_context == "loan_amount":
            out["loan_amount_requested"] = amount
        elif prompt_context == "existing_emi":
            out["existing_emi"] = amount
        else:
            # heuristic: small amounts (<= 5L) likely income, larger likely loan
            if amount <= 500000 and "loan" not in text.lower() and "emi" not in text.lower():
                out["monthly_income"] = amount
            elif "loan" in text.lower() or "borrow" in text.lower() or "need" in text.lower():
                out["loan_amount_requested"] = amount

    if prompt_context == "consent":
        out["consent"] = detect_consent(text)

    return out
