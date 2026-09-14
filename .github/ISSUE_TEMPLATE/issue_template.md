---
name: Issue Template
about: Unified schema for reporting bugs, proposing features, or filing tasks
title: '<repo/area>: <imperative summary>'
labels: 'lane:kernel, prio:P2, kind:feat, size:S, state:ready'
---

## Context & Problem Statement
<!-- A clear, concise description of the bug, requirement, or architectural gap. -->

## Acceptance Criteria
<!-- Specific, testable requirements that define success. -->
- [ ] Criterion 1: Specific, testable requirement.
- [ ] Criterion 2: Measurable verification step.

## Technical Seams & Implementation Outline
<!-- Key modules, contracts, or functions to modify (e.g., `myfleet.fleet_ask`). -->
- Key files/functions: 
- Preserved invariants:

## Verification & Reproduction
<!-- Commands to verify locally. -->
```bash
pytest tests/
ruff check .
```

## Related Issues & Dependencies
- Closes #
- Depends on #
