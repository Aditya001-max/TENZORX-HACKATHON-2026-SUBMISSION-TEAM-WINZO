
from __future__ import annotations
from dataclasses import dataclass
from typing import Dict, Any, List, Optional

try:
    from . import nlu  # when imported as part of a package
except ImportError:  # pragma: no cover
    import nlu  # when running flat (uvicorn main:app from backend/)


# ---------------- slot definitions ----------------

@dataclass
class Slot:
    key: str
    question: str
    context: str          # prompt_context passed to nlu.extract_all
    required: bool = True
    confirm_message: str = ""


SLOTS: List[Slot] = [
    Slot("full_name",
         "Hello! I'm Maya, your loan assistant from Poonawalla Fincorp. "
         "May I please have your full name as per your PAN card?",
         "name",
         confirm_message="Thank you, {full_name}."),

    Slot("declared_age",
         "Could you please tell me your age?",
         "age",
         confirm_message="Got it — {declared_age} years."),

    Slot("employment_type",
         "What is your current employment type? "
         "For example, are you salaried, self-employed, or a business owner?",
         "employment",
         confirm_message="Noted — {employment_type}."),

    Slot("monthly_income",
         "What is your approximate monthly income in rupees?",
         "income",
         confirm_message="Recorded monthly income of ₹{monthly_income}."),

    Slot("loan_purpose",
         "What is the primary purpose of this loan? "
         "For example: home renovation, wedding, medical, education or business.",
         "purpose",
         confirm_message="Understood — loan purpose is {loan_purpose}."),

    Slot("loan_amount_requested",
         "How much loan amount would you like to apply for?",
         "loan_amount",
         confirm_message="You'd like to apply for ₹{loan_amount_requested}."),

    Slot("pan",
         "Could you please state your 10-character PAN number?",
         "pan",
         required=False,
         confirm_message="PAN captured."),

    Slot("consent",
         "Before I generate your personalised loan offer, I need your verbal consent. "
         "Do you agree to share the information collected during this call with "
         "Poonawalla Fincorp for credit assessment, in line with RBI guidelines? "
         "Please clearly say 'Yes, I agree' or 'No'.",
         "consent",
         confirm_message="Consent recorded."),
]


# ---------------- agent ----------------

INTRO_MESSAGE = (
    "Hi! Welcome to the Poonawalla Fincorp Loan Wizard. "
    "This short video call will help us understand your needs and "
    "instantly generate a personalised loan offer for you. "
    "The session is recorded for compliance. Let's begin."
)

CLOSING_APPROVED = (
    "Excellent! Based on what you've shared, you've been pre-approved. "
    "Your personalised offer is now displayed on your screen. "
    "Thank you for choosing Poonawalla Fincorp."
)

CLOSING_REFER = (
    "Thank you for your responses. Your application requires a brief manual review. "
    "Our team will reach out within 24 hours with your offer. "
    "A preliminary offer is shown on your screen for reference."
)

CLOSING_REJECT = (
    "Thank you for your time. Based on the information shared, we are unable "
    "to extend a loan offer at this moment. The detailed reasons have been "
    "shared on your screen and a copy will be emailed to you."
)


class LoanAgent:
    def __init__(self):
        self.conversation_log: List[Dict[str, str]] = []

    # ----- public API -----

    def greet(self) -> Dict[str, Any]:
        """First message of the session."""
        msg = INTRO_MESSAGE + " " + SLOTS[0].question
        return {
            "agent_message": msg,
            "next_slot": SLOTS[0].key,
            "next_context": SLOTS[0].context,
            "stage": "intro",
            "completed": False,
        }

    def step(self,
             customer_text: str,
             current_slot: str,
             collected: Dict[str, Any]) -> Dict[str, Any]:
        """
        Process the customer's answer to the current slot, decide the next slot,
        and return everything the frontend needs to advance the UI.
        """
        slot = self._slot(current_slot)
        ctx = slot.context if slot else None

        extracted = nlu.extract_all(customer_text, prompt_context=ctx)

        # Special handling for consent slot - only when *that* slot is being asked
        if current_slot == "consent":
            consent_given = nlu.detect_consent(customer_text)
            extracted["consent"] = consent_given

        # Merge new info into collected store
        new_info: Dict[str, Any] = {}
        for k, v in extracted.items():
            if v is not None and v != "":
                if k not in collected or not collected.get(k):
                    new_info[k] = v
                    collected[k] = v

        # Confirmation phrase
        confirm = ""
        if slot and slot.key in collected and slot.confirm_message:
            try:
                # build a display-friendly version of collected
                disp = dict(collected)
                for k in ("monthly_income", "loan_amount_requested", "existing_emi"):
                    if isinstance(disp.get(k), (int, float)):
                        disp[k] = f"{int(disp[k]):,}"
                confirm = slot.confirm_message.format(**disp)
            except Exception:
                confirm = ""

        # Was this slot satisfied?
        slot_filled = bool(slot and (slot.key in collected and collected.get(slot.key)
                                      not in (None, "", False) or
                                      (slot.key == "consent" and "consent" in collected)))

        # If this slot is OPTIONAL and the customer clearly skipped it,
        # mark it as attempted (None) so we don't loop forever.
        if slot and not slot.required and not slot_filled:
            skip_words = ["skip", "don't have", "do not have", "dont have", "no pan",
                          "later", "not now", "i don't", "i do not"]
            if any(w in customer_text.lower() for w in skip_words):
                collected[slot.key] = None
                slot_filled = True

        # Find next missing required slot
        next_slot = self._next_required_slot(collected)

        if next_slot is None:
            # All slots done — produce the closing trigger
            return {
                "agent_message": confirm + " I have all the information I need. "
                                 "Please wait while I evaluate your profile…",
                "next_slot": None,
                "next_context": None,
                "stage": "evaluating",
                "completed": True,
                "collected": collected,
                "new_info": new_info,
            }

        # If the customer didn't answer the asked slot at all, gently re-ask
        if not slot_filled and slot is not None:
            reprompt = self._reprompt(slot, customer_text)
            return {
                "agent_message": reprompt,
                "next_slot": slot.key,
                "next_context": slot.context,
                "stage": "collecting",
                "completed": False,
                "collected": collected,
                "new_info": new_info,
                "reprompt": True,
            }

        msg = (confirm + " " if confirm else "") + next_slot.question
        return {
            "agent_message": msg.strip(),
            "next_slot": next_slot.key,
            "next_context": next_slot.context,
            "stage": "collecting",
            "completed": False,
            "collected": collected,
            "new_info": new_info,
        }

    def closing_message(self, decision: str) -> str:
        return {
            "APPROVE": CLOSING_APPROVED,
            "REFER":   CLOSING_REFER,
            "REJECT":  CLOSING_REJECT,
        }.get(decision, CLOSING_REFER)

    # ----- helpers -----

    def _slot(self, key: Optional[str]) -> Optional[Slot]:
        if not key:
            return None
        for s in SLOTS:
            if s.key == key:
                return s
        return None

    def _next_required_slot(self, collected: Dict[str, Any]) -> Optional[Slot]:
        """
        Iterate slots in declaration order. Consent is always the LAST step.
        Optional slots (e.g. PAN) are still asked once, before consent,
        unless the customer has refused them earlier in the conversation.
        """
        # First pass: any non-consent slot still missing
        for s in SLOTS:
            if s.key == "consent":
                continue
            if s.key not in collected or not collected.get(s.key):
                # Required slots are mandatory; optional slots are asked once
                # (we mark them as "asked" by storing None to avoid loops).
                if s.required:
                    return s
                # optional slot not yet attempted
                if s.key not in collected:
                    return s
        # Finally consent
        if "consent" not in collected:
            return self._slot("consent")
        return None

    def _reprompt(self, slot: Slot, customer_text: str) -> str:
        if slot.key == "monthly_income":
            return ("I couldn't quite catch the income figure. "
                    "Please tell me your monthly income — for example, "
                    "you can say 'forty thousand rupees' or '40000'.")
        if slot.key == "declared_age":
            return "Sorry, could you please clearly state your age in years?"
        if slot.key == "consent":
            return ("I need a clear yes or no for compliance. "
                    "Do you consent to share this information with Poonawalla Fincorp?")
        if slot.key == "pan":
            return ("Please spell out your PAN number — it is 10 characters: "
                    "five letters, four digits, then one letter.")
        return f"Sorry, I didn't catch that. {slot.question}"


# ---------------------------------------------------------------
# Module-level singleton + thin wrappers, so callers can simply do
#   agent.greet(), agent.step(...), agent.closing_message(...)
# without having to manage the LoanAgent instance themselves.
# ---------------------------------------------------------------

_AGENT_SINGLETON: Optional["LoanAgent"] = None


def _instance() -> "LoanAgent":
    global _AGENT_SINGLETON
    if _AGENT_SINGLETON is None:
        _AGENT_SINGLETON = LoanAgent()
    return _AGENT_SINGLETON


def greet() -> Dict[str, Any]:
    return _instance().greet()


def step(customer_text: str,
         current_slot: str,
         collected: Dict[str, Any]) -> Dict[str, Any]:
    return _instance().step(customer_text, current_slot, collected)


def closing_message(decision: str) -> str:
    return _instance().closing_message(decision)
