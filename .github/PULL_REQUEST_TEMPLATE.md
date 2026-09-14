## Summary of Changes
<!-- A clear, concise description of the changes made and why. -->

Closes #<!-- issue number -->

## Empirical Verification Output
<!-- Include exact test and lint output demonstrating clean pass. -->
```
pytest output:
ruff check output:
```

## Safety Checklist
- [ ] Changes land on a non-main branch via PR.
- [ ] Required CI checks run against worktree diff (`pythonpath = ["src"]`).
- [ ] No secrets, tokens, or credentials persisted.
- [ ] No harness/rules, CI workflows, credentials, or public API/schema migrations changed without human approval.
- [ ] PR is opened as Draft first; marked ready only when tests pass green.
