---
name: osteoblast-severity-routing
description: Classify Osteoblast findings into routine or serious work and apply the correct local-versus-cloud remediation posture.
---

# Osteoblast Severity Routing

Use this skill when deciding whether a finding should be handled locally or escalated to GitHub Copilot cloud agent.

## Routine findings

Choose `routine` when the fix is small, locally provable, and stays within the repository's maintenance budget.

Typical examples:

- dead code removal
- stale documentation updates
- naming or consistency fixes
- narrow bug fixes with local proof
- straightforward hardening at clear system boundaries

## Serious findings

Choose `serious` when any of these are true:

- the expected change exceeds the configured file or line budget
- the finding touches auth, security, permissions, or secrets
- the finding touches public API surface
- the finding touches workflows, infrastructure, schema, or migrations
- confidence is below the configured threshold
- the safe fix clearly needs architectural discussion

## Routing rules

- `routine`: fix locally with the worker agent and open one PR.
- `serious`: do not edit locally; create a tracking issue and delegate to GitHub Copilot cloud agent.
