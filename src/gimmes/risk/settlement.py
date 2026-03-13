"""Settlement risk scanner — red-flag keyword detection."""

from __future__ import annotations

from dataclasses import dataclass

# Red flags that indicate settlement discretion or ambiguity
RED_FLAG_KEYWORDS = [
    "sole discretion",
    "may determine",
    "reserves the right",
    "carveout",
    "carve-out",
    "carve out",
    "death",
    "incapacitation",
    "at its discretion",
    "subjective",
    "may cancel",
    "may void",
    "emergency",
    "force majeure",
    "suspend trading",
    "extend expiration",
    "ambiguous",
    "unclear",
    "may resolve",
    "interpretation",
]


@dataclass
class SettlementRisk:
    """Result of settlement risk analysis."""

    is_clear: bool
    red_flags: list[str]
    risk_level: str  # "low", "medium", "high"

    @property
    def summary(self) -> str:
        if not self.red_flags:
            return "Settlement rules appear clear."
        flags = ", ".join(self.red_flags)
        return f"Settlement risk ({self.risk_level}): found [{flags}]"


def scan_settlement_rules(rules_text: str) -> SettlementRisk:
    """Scan market settlement rules for red-flag keywords.

    Args:
        rules_text: The market's rules/settlement language.

    Returns:
        SettlementRisk with found red flags and overall assessment.
    """
    if not rules_text:
        return SettlementRisk(
            is_clear=False,
            red_flags=["no settlement rules provided"],
            risk_level="high",
        )

    text_lower = rules_text.lower()
    found_flags = [kw for kw in RED_FLAG_KEYWORDS if kw in text_lower]

    if len(found_flags) >= 3:
        risk_level = "high"
    elif len(found_flags) >= 1:
        risk_level = "medium"
    else:
        risk_level = "low"

    return SettlementRisk(
        is_clear=len(found_flags) == 0,
        red_flags=found_flags,
        risk_level=risk_level,
    )
