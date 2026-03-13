---
name: research
description: Deep research on gimme candidates using the Caddie agent
user_invocable: true
---

# /research — Deep Research

Dispatch the Caddie agent to perform deep research on gimme candidates.

## Instructions

Launch the Caddie agent (`caddie.md`) to:
1. Take the Scout's shortlist (or a specific ticker if provided as argument)
2. Research each candidate's underlying event
3. Estimate true probability with confidence signals
4. Produce a GimmeScore and research memo
5. Recommend PROCEED, PASS, or NEEDS MORE RESEARCH

If an argument is provided (e.g., `/research KXCPI-26MAR-T3.2`), research only that specific ticker.
