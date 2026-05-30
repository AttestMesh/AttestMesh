# Code Generator

Generate implementation code from specifications.

Code is derived from specs. Every generated file should trace back to its source specification. This command bridges the gap between "what we want" (spec) and "how we build it" (code).

## Prerequisites

- Spec must exist in `docs/specs/`
- Spec status should be APPROVED (warn if DRAFT)
- Project must have established patterns to follow

---

## Process

### 1. Read and Validate the Spec

```bash
Read: docs/specs/<spec-name>.md
```

Check:
- [ ] Status is APPROVED (or warn if DRAFT)
- [ ] Requirements are clear and testable
- [ ] Design section exists
- [ ] No unresolved Open Questions (or flag them)

If Open Questions exist:
> "This spec has unresolved questions: [list]. Proceed anyway, or resolve first?"

### 2. Analyze Existing Codebase

Before generating, understand the project's patterns:

**File Organization:**
- Where do similar features live?
- What's the directory structure convention?

**Code Patterns:**
- Naming conventions (camelCase, snake_case, etc.)
- Error handling patterns
- Testing patterns
- Import organization

**Architecture:**
- Dependency injection? Global state?
- Interface-first? Concrete types?
- Monolith? Microservices? Modules?

Use Glob and Grep to find examples:
```bash
# Find similar implementations
Glob: **/*auth*.go  (or .ts, .py, etc.)
Grep: "func.*Handler" or similar patterns
```

### 3. Plan the Implementation

Before writing code, present a plan:

```
Implementation Plan for: <spec-name>

Files to create:
1. src/auth/handler.go — HTTP handlers
2. src/auth/service.go — Business logic
3. src/auth/repository.go — Data access
4. src/auth/types.go — Type definitions

Files to modify:
1. src/router.go — Add routes
2. src/app.go — Wire dependencies

Tests to create:
1. src/auth/handler_test.go
2. src/auth/service_test.go

Estimated scope: ~400 lines of implementation, ~300 lines of tests
```

**STOP and wait for user approval of the plan.**

For complex implementations, use EnterPlanMode.

### 4. Generate Code with Traceability

Every generated file includes a traceability header:

**Go:**
```go
// Code generated from spec: docs/specs/user-auth.md
// Implements: Requirements section, items 1-5
// Generated: 2025-01-20

package auth
```

**TypeScript:**
```typescript
/**
 * @spec docs/specs/user-auth.md
 * @implements Requirements 1-5
 * @generated 2025-01-20
 */
```

**Python:**
```python
"""
Spec: docs/specs/user-auth.md
Implements: Requirements 1-5
Generated: 2025-01-20
"""
```

### 5. Inline Requirement References

For complex logic, reference specific requirements:

```go
// Implements: docs/specs/user-auth.md#password-requirements
// - Minimum 8 characters
// - At least one uppercase, one lowercase, one number
func ValidatePassword(password string) error {
    if len(password) < 8 {
        return ErrPasswordTooShort
    }
    // ...
}
```

### 6. Generate Tests

For each implementation file, generate corresponding tests:

- Unit tests for business logic
- Integration tests for data access
- Handler tests for API endpoints

Tests should reference what they're testing:

```go
// Tests: docs/specs/user-auth.md#requirement-3
func TestPasswordValidation(t *testing.T) {
    // ...
}
```

### 7. Update Spec Traceability

After generating, update the spec's Traceability section:

```markdown
## Traceability

| Requirement | Implementation | Tests |
|-------------|----------------|-------|
| Password validation | src/auth/validator.go:15 | src/auth/validator_test.go |
| Session management | src/auth/session.go | src/auth/session_test.go |
| OAuth integration | src/auth/oauth.go | src/auth/oauth_test.go |
```

Update spec status to IMPLEMENTING:
```markdown
**Status:** IMPLEMENTING
```

### 8. Build and Test

Run the project's build and test commands:

```bash
# Build
<project build command from CLAUDE.md>

# Test
<project test command from CLAUDE.md>
```

Report results:
- Build: PASS/FAIL
- Tests: X passed, Y failed, Z skipped

### 9. User Approval

Present summary:
```
Generated implementation for: <spec-name>

Files created: 5
Files modified: 2
Lines of code: ~400
Lines of tests: ~300

Build: PASS
Tests: 12 passed, 0 failed

Ready to commit?
```

**STOP and wait for approval.**

### 10. Commit

Stage and commit with descriptive message:

```bash
git add <files>
git commit -m "Implement <feature> per docs/specs/<spec>.md

- Add <component 1>
- Add <component 2>
- Update <modified files>

Spec: docs/specs/<spec>.md"
```

---

## Anti-Patterns to Avoid

### Over-Engineering
- Don't add features not in the spec
- Don't create abstractions for single use cases
- Don't add "future-proofing" not requested

### Under-Specifying
- If the spec is vague, ask questions BEFORE generating
- Don't guess at requirements
- Don't fill gaps with assumptions

### Ignoring Context
- Don't introduce new patterns when existing ones work
- Don't ignore project conventions
- Don't create inconsistency

---

## Incremental Generation

For large specs, generate incrementally:

```
/generate docs/specs/big-feature.md --section requirements.1-3
```

This generates only the specified requirements, allowing iterative implementation and review.

---

## Regeneration

When a spec is updated (status: REVISED):

1. Read the changelog to understand what changed
2. Identify affected code sections via traceability
3. Propose targeted updates (not full regeneration)
4. Update traceability table

---

## Examples

**Example 1: Generate from approved spec**
```
/generate docs/specs/user-authentication.md
```
(Full workflow: plan, generate, test, commit)

**Example 2: Generate specific section**
```
/generate docs/specs/api-v2.md --section endpoints.users
```
(Partial implementation)

**Example 3: Check what would be generated**
```
/generate docs/specs/feature.md --dry-run
```
(Shows plan without generating)

**Example 4: Regenerate after spec update**
```
/generate docs/specs/feature.md --update
```
(Incremental update based on spec changes)
