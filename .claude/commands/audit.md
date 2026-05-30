# Project Audit

Comprehensive health check for any project. Configurable based on project type and structure.

## Purpose

Audits answer: "Is this project in a healthy state?"

- Can it build?
- Do tests pass?
- Is documentation current?
- Are there obvious issues?
- What needs attention?

---

## Configuration

Audits are configured via CLAUDE.md or a `holodeck.yaml` file:

```yaml
# holodeck.yaml (optional)
audit:
  build_command: "go build ./..."
  test_command: "go test ./... -short"
  lint_command: "golangci-lint run"

  directories:
    source: "src/"
    tests: "src/"
    specs: "docs/specs/"
    docs: "docs/"

  checks:
    - build
    - tests
    - lint
    - specs
    - docs
    - reconciliation

  ignore:
    - "vendor/"
    - "node_modules/"
    - ".git/"
```

If no config exists, the audit infers from project structure.

---

## Audit Storage & Retention

Audits are stored in `docs/audits/` with timestamp-based naming:

```
docs/audits/AUDIT_<unix_timestamp>.md
```

**Retention Policy:** Latest audit only. Git history provides access to previous audits. Delete the previous audit file when creating a new one.

---

## Parallel Execution

**Use parallel agents for efficiency** when auditing multiple components. Launch separate agents for independent checks (build, tests, specs, docs) simultaneously rather than sequentially.

---

## Audit Categories

### 1. Build Health

**Check:** Does the project compile/build?

```bash
<build_command from CLAUDE.md or config>
```

**Output:**
```
Build: PASS
  Duration: 4.2s
  Warnings: 3
```

or

```
Build: FAIL
  Error: src/handler.go:45: undefined: NewService
  Fix: Import missing or function renamed
```

### 2. Test Health

**Check:** Do tests pass?

```bash
<test_command from CLAUDE.md or config>
```

**Output:**
```
Tests: PASS
  Total: 156
  Passed: 154
  Skipped: 2
  Failed: 0
  Coverage: 78%
```

### 3. Lint/Static Analysis

**Check:** Code quality issues?

```bash
<lint_command if configured>
```

**Output:**
```
Lint: 5 issues
  - src/handler.go:23: error return value not checked
  - src/service.go:45: unused variable 'tmp'
  ...
```

### 4. Specification Health

**Check:** Are specs in good shape?

- Any DRAFT specs older than 2 weeks?
- Any APPROVED specs with no implementation?
- Any IMPLEMENTING specs stalled?
- Traceability tables complete?

**Output:**
```
Specs: 2 issues

STALE DRAFTS:
  - docs/specs/old-feature.md (created 45 days ago, still DRAFT)

APPROVED BUT UNIMPLEMENTED:
  - docs/specs/rate-limiting.md (approved 14 days ago)
```

### 5. Documentation Health

**Check:** Is documentation current?

- README exists and has required sections?
- API docs match implementation?
- No broken internal links?
- No references to non-existent files?

**Output:**
```
Docs: 1 issue

BROKEN REFERENCES:
  - README.md:67 references src/old.go (not found)
```

### 6. Reconciliation Summary

**Check:** Quick consistency check (see /reconcile for full)

- Spec coverage percentage
- Untraced code percentage
- Documentation accuracy

**Output:**
```
Reconciliation: HEALTHY
  Spec coverage: 94%
  Untraced code: 12% (mostly utilities)
  Doc accuracy: 100%
```

### 7. Security (if configured)

**Check:** Obvious security issues?

- Secrets in code?
- Known vulnerable dependencies?
- Unsafe patterns?

```bash
<security_scan_command if configured>
```

### 8. Dependencies

**Check:** Dependency health

- Outdated dependencies?
- Known vulnerabilities?
- Unused dependencies?

---

## Output Format

### Summary View (default)

```
================================================================================
                           PROJECT AUDIT REPORT
                           <project-name>
                           <timestamp>
================================================================================

SUMMARY
-------
Overall Health: GOOD (2 minor issues)

| Category      | Status | Issues |
|---------------|--------|--------|
| Build         | PASS   | 0      |
| Tests         | PASS   | 0      |
| Lint          | WARN   | 5      |
| Specs         | PASS   | 0      |
| Docs          | WARN   | 1      |
| Reconciliation| PASS   | 0      |

ISSUES
------
1. [WARN] Lint: 5 style issues (non-blocking)
2. [WARN] Docs: README references non-existent file

RECOMMENDATIONS
---------------
1. Fix lint issues before next release
2. Update README.md line 67

================================================================================
```

### Detailed View (`/audit --verbose`)

Full output from each check, not just summary.

### Machine-Readable (`/audit --json`)

```json
{
  "timestamp": "2025-01-20T10:30:00Z",
  "project": "my-project",
  "overall": "GOOD",
  "categories": {
    "build": {"status": "PASS", "issues": []},
    "tests": {"status": "PASS", "passed": 156, "failed": 0},
    ...
  }
}
```

---

## Audit Report Storage

Audits can be saved for historical tracking:

```bash
/audit --save
```

Saves to: `docs/audits/AUDIT_<timestamp>.md`

This enables:
- Tracking health over time
- Pre-release audit trail
- Onboarding context

---

## Quick Checks

For rapid feedback during development:

```
/audit --quick
```

Only runs:
- Build
- Tests (short mode)
- Basic lint

Skips:
- Full reconciliation
- Documentation check
- Security scan

---

## Custom Audit Rules

Projects can define custom checks in CLAUDE.md:

```markdown
## Custom Audit Rules

1. **Proto Sync**: All .proto files must have generated .pb.go files
2. **Migration Check**: Each migration must have an up and down
3. **Config Validation**: Config files must pass schema validation
```

The audit command will attempt to verify these rules.

---

## Interpreting Results

### Overall Health Ratings

| Rating | Meaning |
|--------|---------|
| **EXCELLENT** | All checks pass, no issues |
| **GOOD** | All critical checks pass, minor issues only |
| **FAIR** | Some issues need attention, core functionality OK |
| **POOR** | Significant issues, some features may not work |
| **CRITICAL** | Build fails or tests fail, immediate attention needed |

### Issue Severity

| Severity | Action Required |
|----------|-----------------|
| **CRITICAL** | Must fix before any other work |
| **HIGH** | Fix before next release |
| **MEDIUM** | Fix when convenient |
| **LOW** | Nice to fix, not urgent |
| **INFO** | Informational only |

---

## Examples

**Example 1: Standard audit**
```
/audit
```
(Full audit with summary output)

**Example 2: Quick check during development**
```
/audit --quick
```
(Build + tests only)

**Example 3: Detailed output**
```
/audit --verbose
```
(Full output from all checks)

**Example 4: Pre-release audit**
```
/audit --save
```
(Full audit, saved to docs/audits/)

**Example 5: CI-friendly output**
```
/audit --json
```
(Machine-readable for CI integration)
