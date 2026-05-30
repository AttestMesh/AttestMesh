# Specification Manager

Create and refine specifications before writing code.

Specs are the source of truth. Code implements specs. This command helps you think through what you're building before you build it.

## Modes

| Mode | Command | Description |
|------|---------|-------------|
| **Create** | `/spec create <name>` | Start a new specification |
| **Refine** | `/spec refine <file>` | Iterate on an existing spec |
| **Status** | `/spec status` | List all specs and their status |
| **Split** | `/spec split <file>` | Break a large spec into components |

---

## Spec Lifecycle

```
┌─────────┐    ┌─────────┐    ┌──────────┐    ┌──────────────┐    ┌─────────────┐
│  DRAFT  │ -> │ REVIEW  │ -> │ APPROVED │ -> │ IMPLEMENTING │ -> │ IMPLEMENTED │
└─────────┘    └─────────┘    └──────────┘    └──────────────┘    └─────────────┘
     │                                                                    │
     └─────────────────────── REVISED <──────────────────────────────────┘
```

**Status Definitions:**
- **DRAFT**: Being written, not ready for review
- **REVIEW**: Ready for stakeholder feedback
- **APPROVED**: Locked for implementation
- **IMPLEMENTING**: Code being written
- **IMPLEMENTED**: Code complete, spec is historical record
- **REVISED**: Spec updated after implementation (major changes)

---

## Create Mode

### Process

1. **Understand the Feature**

   Ask clarifying questions if the request is vague:
   - What problem does this solve?
   - Who is the user?
   - What does success look like?

2. **Research Context**

   Before drafting:
   - Read related specs (if any exist)
   - Check existing code patterns
   - Identify dependencies

3. **Draft the Spec**

   Use the spec template (see below) with these sections:
   - Overview (the "why")
   - Requirements (must-haves)
   - Non-Requirements (explicit scope boundaries)
   - Design (the "how")
   - Open Questions (unresolved decisions)
   - Traceability (to be filled during implementation)

4. **Apply Collaborative Refinement**

   Use standard option generation patterns:
   - Cast wide on approaches
   - Offer inversions
   - Fusion on request
   - Flag risks proactively

5. **Write the Spec File**

   Output location: `docs/specs/<name>.md`

   Initial status: `DRAFT`

### Spec Template

```markdown
# <Feature Name>

**Status:** DRAFT
**Author:** <author>
**Created:** <date>
**Last Updated:** <date>

## Overview

<1-2 paragraphs explaining what this feature does and why it matters>

## Requirements

### Must Have
- [ ] <requirement 1>
- [ ] <requirement 2>

### Should Have
- [ ] <nice-to-have 1>

### Must NOT Have
- <explicit non-requirement to prevent scope creep>

## Non-Requirements

<What is explicitly OUT of scope for this spec>

## Design

### Architecture

<How the feature fits into the system>

### Components

<What pieces need to be built>

### Interfaces

<APIs, data formats, protocols>

### Data Model

<New data structures or schema changes>

## Open Questions

- [ ] <Decision that needs to be made>
- [ ] <Uncertainty that needs research>

## Alternatives Considered

### <Alternative 1>
<Why it was rejected>

### <Alternative 2>
<Why it was rejected>

## Traceability

*Filled in during implementation*

| Requirement | Implementation | Tests |
|-------------|----------------|-------|
| | | |

## Changelog

| Date | Author | Changes |
|------|--------|---------|
| <date> | <author> | Initial draft |
```

---

## Refine Mode

### Process

1. **Read the Current Spec**

   Understand what's already written.

2. **Identify Refinement Type**

   - **Clarification**: Something is ambiguous
   - **Addition**: Missing requirements discovered
   - **Removal**: Over-scoped, need to cut
   - **Restructure**: Organization isn't working
   - **Decision**: Open question needs resolution

3. **Apply Collaborative Refinement**

   Same patterns as /doc:
   - Option generation for approaches
   - Inversions unprompted
   - Fusion on request
   - Decision compression for rapid iteration

4. **Update Status**

   If the spec was APPROVED and changes are significant:
   - Change status to REVISED
   - Document the change in Changelog
   - Consider if implementation needs updating

### Refinement Triggers

Watch for these signals that a spec needs refinement:
- "Wait, what about...?" — Missing requirement
- "This doesn't make sense" — Clarity issue
- "We don't need this" — Over-scoping
- "How would X work?" — Missing design detail
- Contradiction between sections

---

## Status Mode

List all specs with their current status:

```bash
find docs/specs -name "*.md" -exec grep -l "Status:" {} \;
```

Output format:
```
Specifications:

DRAFT (2):
  - docs/specs/user-auth.md (created 2025-01-15)
  - docs/specs/rate-limiting.md (created 2025-01-18)

IMPLEMENTING (1):
  - docs/specs/api-v2.md (approved 2025-01-10)

IMPLEMENTED (5):
  - docs/specs/core-module.md
  - ...
```

---

## Split Mode

When a spec becomes too large:

1. **Identify Natural Boundaries**
   - Separate concerns
   - Independent deliverables
   - Different teams/owners

2. **Create Child Specs**
   - Reference parent spec
   - Inherit relevant context
   - Own their own lifecycle

3. **Update Parent Spec**
   - Change to "umbrella" spec
   - Link to child specs
   - Track overall progress

---

## Best Practices

### Writing Good Requirements

**Bad:** "The system should be fast"
**Good:** "API responses must complete in <100ms for 95th percentile"

**Bad:** "Users can log in"
**Good:** "Users can authenticate via email/password or OAuth (Google, GitHub)"

### Scoping Effectively

Every spec should have a "Non-Requirements" section. This prevents:
- Scope creep during implementation
- Misaligned expectations
- Gold-plating

### When to Split

Split a spec when:
- It exceeds 500 lines
- It covers multiple independent systems
- Different parts have different timelines
- Review becomes unwieldy

---

## Decision Points Summary

**ALWAYS STOP and wait for user response at these points:**
- The feature request is ambiguous (ask clarifying questions)
- Multiple valid design approaches exist (present options)
- A decision affects other specs or systems
- Before changing spec status (DRAFT → APPROVED, etc.)
- Before removing or significantly modifying requirements
- Before marking a spec as IMPLEMENTED

**Present options clearly** when decisions are needed. Do not assume user intent.

---

## Examples

**Example 1: Create a new spec**
```
/spec create user-authentication
```
(Triggers: clarifying questions, context research, draft with template)

**Example 2: Refine an existing spec**
```
/spec refine docs/specs/user-authentication.md
```
(Triggers: read spec, identify issues, collaborative refinement)

**Example 3: Check project status**
```
/spec status
```
(Triggers: scan all specs, group by status)

**Example 4: Break up a large spec**
```
/spec split docs/specs/monolith-feature.md
```
(Triggers: identify boundaries, create child specs, update parent)
