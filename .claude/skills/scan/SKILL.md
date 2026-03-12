---
name: scan
description: Scan Kalshi markets for gimme candidates using the Scout agent
user_invocable: true
---

# /scan — Market Scanning

Dispatch the Scout agent to scan Kalshi markets for gimme candidates.

## Instructions

Launch the Scout agent (`scout.md`) to:
1. Run `python -m gimmes scan` to fetch and filter markets
2. Score the top candidates
3. Return a ranked shortlist

The Scout will output a structured shortlist of markets worth researching further.
