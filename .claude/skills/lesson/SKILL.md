---
name: lesson
description: Run strategy analysis and get data-backed parameter recommendations from The Pro
user_invocable: true
---

# /lesson — Strategy Analysis

Run The Pro strategy advisor to analyze trading performance and produce parameter recommendations.

Launch the Pro agent (`pro.md`) to:
1. Assess data availability (need 20+ completed trades)
2. Run all applicable analyses (threshold sweep, edge decay, Kelly optimization, scanner review)
3. Review past recommendations for status changes
4. File GitHub issues for high-confidence recommendations
5. Output "The Lesson" report

**Quick mode (no agent, just the analysis):**
```bash
python -m gimmes lesson
```

**Full agent mode (includes GitHub issue filing and past recommendation tracking):**
Launch the Pro agent for comprehensive analysis with issue filing capability.
