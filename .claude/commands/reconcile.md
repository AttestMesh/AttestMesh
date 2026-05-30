# Reconciliation

Sync specifications, code, and documentation. Detect drift and propose fixes.

Over time, these artifacts naturally diverge:
- Specs get updated but code doesn't follow
- Code evolves but specs aren't updated
- Documentation describes outdated behavior

This command detects and resolves these inconsistencies.

---

## Modes

| Mode | Command | Description |
|------|---------|-------------|
| **All** | `/reconcile all` | Full consistency check across everything |
| **Spec to Code** | `/reconcile spec-to-code` | Check if code implements specs |
| **Code to Spec** | `/reconcile code-to-spec` | Check if specs reflect code |
| **Docs** | `/reconcile docs` | Check if docs match reality |
| **Single** | `/reconcile <file>` | Reconcile one specific file |

---

## Parallel Execution

**Use parallel agents for efficiency** when reconciling multiple artifacts. When checking specs, code, and docs, launch separate search agents for each category simultaneously.

---

## Multi-Document Reconciliation Pattern

When reconciling a narrative or concept across multiple documents, use the **Decision Tree Document** pattern:

### Step 1: Create Master Decision Tree

Create a temporary working document (not committed) containing:

1. **Core Narrative Statement** — The authoritative description all documents must reflect
2. **Affected Documents Inventory** — Table of documents and sections affected
3. **Numbered Changes with File Prefixes** — Use prefixes to identify targets:
   - `README-1`, `README-2`: README changes
   - `SPEC-1`, `SPEC-2`: Specification changes
   - `DOC-1`, `DOC-2`: Documentation changes
   - `CODE-1`, `CODE-2`: Code comment changes

### Step 2: Track Approval Status

Use the approval legend for each change:

| Marker | Meaning |
|--------|---------|
| `[PENDING]` | Awaiting user review |
| `[A]` | Approved as written |
| `[R]` | Rejected (include reason) |
| `[M]` | Modified (include revision) |

### Step 3: Execute in Dependency Order

Apply changes in order of authority:
1. Primary source of truth (whitepaper/main spec)
2. Dependent specifications
3. Documentation
4. README

**Do NOT proceed to dependent docs until primary is approved.**

### Step 4: Commit Hygiene

- **Delete the decision tree document** before committing (it's a working artifact)
- Commit message should describe WHAT changed, not the reconciliation process
- Get user approval on commit message before committing

---

## Special Handling Rules

When reconciling, follow these rules for edge cases:

| Situation | Treatment |
|-----------|-----------|
| **Pending removal** | Write docs AS IF already removed |
| **Unimplemented specs** | Keep in docs but mark as "Planned" or "Roadmap" |
| **Partially implemented** | Document what IS implemented, note gaps |
| **Spec outdated but code exists** | Update spec first, then reconcile docs |
| **Doc describes removed feature** | Remove from docs, note in changelog if significant |

---

## Full Reconciliation (`/reconcile all`)

### Step 1: Gather Artifacts

**Specifications:**
```bash
find docs/specs -name "*.md" | while read spec; do
    # Extract status, requirements, traceability
done
```

**Code with Traceability:**
```bash
grep -r "@spec\|// Spec:\|# Spec:" src/
```

**Documentation:**
- README.md
- docs/*.md
- API documentation
- Whitepapers

### Step 2: Build Relationship Map

```
Spec: docs/specs/auth.md
├── Status: IMPLEMENTED
├── Requirements: 5
├── Traced Code:
│   ├── src/auth/handler.go (3 requirements)
│   ├── src/auth/service.go (2 requirements)
│   └── src/auth/validator.go (1 requirement)
├── Traced Tests:
│   └── src/auth/*_test.go
└── Referenced Docs:
    ├── README.md (line 45)
    └── docs/api.md (lines 120-150)
```

### Step 3: Detect Drift

**Types of Drift:**

| Type | Description | Severity |
|------|-------------|----------|
| **Orphaned Spec** | Spec exists but no code traces to it | HIGH |
| **Orphaned Code** | Code claims spec that doesn't exist | HIGH |
| **Missing Trace** | Requirement in spec, no code reference | MEDIUM |
| **Stale Doc** | Doc describes behavior code doesn't have | MEDIUM |
| **Status Mismatch** | Spec says IMPLEMENTED but code incomplete | HIGH |
| **Outdated Reference** | Doc references moved/renamed files | LOW |

### Step 4: Report Findings

```
Reconciliation Report
=====================

Overall Health: 3 issues found

HIGH SEVERITY (1):
  - docs/specs/rate-limiting.md
    Status: APPROVED (should be IMPLEMENTING or IMPLEMENTED)
    No code traces to this spec
    Action: Implement or revert to DRAFT

MEDIUM SEVERITY (1):
  - docs/specs/auth.md
    Requirement 4 (session expiry) has no code trace
    Last traced code: src/auth/session.go
    Action: Implement or remove requirement

LOW SEVERITY (1):
  - README.md line 67
    References src/old-module.go (file moved to src/module/main.go)
    Action: Update reference
```

### Step 5: Propose Fixes

For each issue, propose a specific fix:

```
Issue: docs/specs/auth.md requirement 4 has no code trace

Options:
A) Implement the requirement
   - Add session expiry logic to src/auth/session.go
   - Estimated: ~30 lines

B) Remove the requirement from spec
   - Mark as "deferred" or delete
   - Update spec changelog

C) Mark as intentionally untraced
   - Add comment explaining why

Which approach?
```

**STOP and wait for user decision on each high/medium issue.**

---

## Spec to Code (`/reconcile spec-to-code`)

Focused check: Do specs have complete implementations?

### Process

1. For each APPROVED or IMPLEMENTED spec:
   - Count requirements
   - Count code traces
   - Flag gaps

2. Output:
```
Spec Coverage Report

docs/specs/auth.md (IMPLEMENTED)
  Requirements: 5
  Traced: 5
  Coverage: 100%

docs/specs/rate-limiting.md (APPROVED)
  Requirements: 3
  Traced: 0
  Coverage: 0% [!]

docs/specs/api-v2.md (IMPLEMENTING)
  Requirements: 8
  Traced: 5
  Coverage: 62%
```

---

## Code to Spec (`/reconcile code-to-spec`)

Reverse check: Does code without spec traces exist?

### Process

1. Find all source files
2. Check for spec traces
3. Flag untraced code

```
Untraced Code Report

Files with no spec reference:
  - src/utils/helpers.go (utility, may be OK)
  - src/legacy/old_handler.go [!] (business logic, needs spec)
  - src/config/loader.go (infrastructure, may be OK)

Files with broken spec references:
  - src/feature/impl.go references docs/specs/old-name.md (not found)
```

---

## Documentation Reconciliation (`/reconcile docs`)

Check if documentation matches reality.

### Process

1. **API Documentation**
   - Compare documented endpoints to actual routes
   - Check request/response schemas match code

2. **README**
   - Verify installation steps work
   - Check feature list matches implemented features

3. **Architecture Docs**
   - Verify described components exist
   - Check diagrams match code structure

4. **Whitepapers**
   - Technical claims match implementation
   - Planned features marked appropriately

```
Documentation Accuracy Report

README.md:
  - "Installation" section: VERIFIED (steps work)
  - "Features" section: 1 STALE (lists unimplemented feature)
  - "API" section: OUTDATED (3 endpoints changed)

docs/api.md:
  - 15 endpoints documented
  - 14 match implementation
  - 1 endpoint removed from code but still documented

docs/whitepaper.tex:
  - Section 5.2 describes "Phase 2" as "planned"
  - Code shows Phase 2 is implemented
  - Action: Update status to "complete"
```

---

## Single File Reconciliation (`/reconcile <file>`)

Reconcile one specific file against its references.

**For a spec file:**
- Find all code that traces to it
- Verify coverage
- Check status accuracy

**For a code file:**
- Find spec it references
- Verify it matches spec requirements
- Check for drift

**For a doc file:**
- Find all claims about code/features
- Verify each claim
- Flag inaccuracies

---

## Automated Fixes

For low-severity issues, offer batch fixes:

```
5 low-severity issues found:

1. README.md:45 - file reference outdated
2. README.md:67 - file reference outdated
3. docs/api.md:23 - endpoint path changed
4. docs/api.md:89 - parameter name changed
5. src/handler.go:12 - spec path changed

Apply all automated fixes? (yes/no)
```

For high/medium severity, always require individual approval.

---

## Best Practices

### Regular Reconciliation
Run `/reconcile all` before:
- Major releases
- Documentation updates
- Onboarding new team members

### Traceability Discipline
When writing code:
- Always include spec reference if implementing a spec
- Update traceability table when complete

When updating specs:
- Change status appropriately
- Update changelog
- Note if code needs updating

---

## Examples

**Example 1: Full check**
```
/reconcile all
```
(Complete consistency analysis)

**Example 2: Pre-release check**
```
/reconcile spec-to-code
```
(Ensure all specs are implemented)

**Example 3: After refactoring**
```
/reconcile code-to-spec
```
(Ensure refactored code still traces correctly)

**Example 4: Single file**
```
/reconcile docs/specs/auth.md
```
(Check this spec against its implementations)
