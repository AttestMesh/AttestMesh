# Session Warmup

Initialize session context by reading key project files.

---

## Execute Now

**Step 1: Read core documentation in parallel**

Use parallel reads for these files:
- `CLAUDE.md` — Project guide, expertise profile, critical directives
- `README.md` — Project overview
- `emh/philosophy.md` (if in Holodeck repo)
- Latest file in `docs/audits/` (if exists)

**Step 2: Check project state**

Run these commands:
```bash
git status
git log --oneline -10
```

**Step 3: Scan for active specifications**

If `docs/specs/` exists:
```bash
ls docs/specs/
```

Read any specs with status DRAFT or IMPLEMENTING.

**Step 4: Report context summary**

After reading, provide:

```
Session initialized for <project name>.

Current state:
- Branch: <current branch>
- Last commit: <commit message>
- Active specs: <list or "none">
- Outstanding issues: <from audit or "none identified">

Ready to proceed. What would you like to work on?
```

---

## Constraints

- Do NOT make any changes during warmup
- Do NOT start implementing without user direction
- Do NOT skip reading CLAUDE.md — it contains critical directives