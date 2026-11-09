You are the root orchestrator.

Goal:
- analyze exactly three repositories under `./repos`
- spawn exactly three foreground subagents
- assign one repository per subagent
- do not analyze any repository inline in the root agent

Execution contract:
1. Launch exactly three subagents, one for each of:
   - `./repos/repo_alpha`
   - `./repos/repo_beta`
   - `./repos/repo_gamma`
2. Each subagent must inspect only its assigned repository.
3. Each subagent must produce:
   - one short finding summary
   - the most relevant file path
   - one concrete fix suggestion
4. The root agent must wait for all three subagents, then write a final report to `./final_report.md`.

Output format for `./final_report.md`:

```md
# Three-Repo Review

## repo_alpha
- finding:
- file:
- fix:

## repo_beta
- finding:
- file:
- fix:

## repo_gamma
- finding:
- file:
- fix:
```

Notes:
- Use subagents explicitly. This task is invalid if fewer or more than three subagents are launched.
- Keep the report concise.
