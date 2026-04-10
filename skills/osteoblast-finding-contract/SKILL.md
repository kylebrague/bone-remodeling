---
name: osteoblast-finding-contract
description: Emit and consume one Osteoblast finding at a time using a strict JSON contract so automation can classify, route, and track repository-maintenance work safely.
---

# Osteoblast Finding Contract

Use this skill whenever you are asked to discover, validate, or execute a repository-maintenance finding for Osteoblast automation.

## Required discovery output

Return exactly one JSON object with these keys:

```json
{
  "type": "osteoblast",
  "category": "hardening",
  "scope": "src/example",
  "proof": ["Evidence from code"],
  "candidate_files": ["src/example/file.ts"],
  "why": "Why this matters",
  "estimated_change_size": {
    "files": 1,
    "lines": 12
  },
  "confidence": 0.9,
  "commit_title": "harden example validation",
  "verification_hint": "Run the focused test or lint command"
}
```

## Rules

- Emit exactly one finding.
- `proof` must cite concrete evidence from the repository.
- `candidate_files` must be the smallest credible set.
- `commit_title` must be short and branch-safe.
- If no acceptable finding exists, return:

```json
{
  "status": "no-finding",
  "reason": "No acceptable single finding was identified in the requested scope."
}
```
