# Document Editor

Edit any document with collaborative refinement patterns.

This command supports both **surgical edits** (when you know exactly what to change) and **collaborative refinement** (when you want to brainstorm and iterate).

## Document Type Detection

Detect the document type from the file path and adjust behavior:

| Extension | Type | Build Command | Special Features |
|-----------|------|---------------|------------------|
| `.tex` | LaTeX | `pdflatex` (twice) | Citation management, PDF output |
| `.md` | Markdown | None (or static site generator) | GitHub-flavored rendering |
| `README.md` | README | None | Project overview conventions |
| `.rst` | reStructuredText | `sphinx-build` | Python docs conventions |
| `.adoc` | AsciiDoc | `asciidoctor` | Technical docs conventions |

---

## Collaborative Refinement

### Option Generation Guidelines

When brainstorming phrasing or content options:

1. **First round: cast wide** — Offer 5-6 options spanning different tones, structures, and lengths. Don't cluster around one approach. If the user rejects all, you miscalibrated the direction, not the wording.

2. **Show complete phrases** — Options should be full sentences/paragraphs ready for evaluation, not fragments. Users pattern-match to rhythm and flow; truncated options don't convey this.

3. **Offer inversions unprompted** — If you propose "A because B," also show "B, therefore A" as an alternative. Structure inversion often unlocks better phrasing.

4. **Fusion is a tool** — When the user likes elements from multiple options, proactively offer 4-6 fusion variants before they ask.

5. **Flag tone risks** — If a phrase could read as dismissive, edgy, or problematic, flag it: "Note: this phrasing could come across as [X]. Consider [alternative]."

### Presenting Options

When iterating on edits:
- **Always show complete context** in each option (if editing one item in a list, show the full list)
- Users need to see how the change flows with surrounding content
- **Name each option** (Option A, B, C or Option 1, 2, 3) for easy reference
- **State your lean** but don't assume — let the user choose direction

### Wordplay and Emphasis

When introducing wordplay or emphasis:
1. **Offer multiple ways to emphasize** (italics, bold, quotes, parentheses)
2. **Show how it renders** — remind user of formatting syntax
3. **Test the effect** — the reader should catch it without explanation

### Iteration Expectation

Good document edits typically require 2-5 rounds of refinement:
- **Round 1**: Direction finding (which approach?)
- **Round 2**: Tone/voice tuning (too wordy? too terse?)
- **Round 3**: Specific word choices (this phrase vs that phrase)
- **Round 4+**: Polish (emphasis, punctuation, flow)

Don't rush to "final text" — stay in brainstorm mode until the user signals satisfaction (e.g., "that one", "yes", "let's do it").

### Tightening/Repetition Audits

When asked to tighten or reduce repetition:

1. **Present as a table** — Theme | Locations | Assessment (keep/cut/merge)
2. **Distinguish earned repetition from redundancy** — Core concepts may deserve repetition; re-explaining the same thing doesn't
3. **Propose cuts in priority order** — Easiest wins first (exact duplicates), then surgical trims
4. **Batch approval** — After presenting cuts, ask "Approve all N cuts?" to reduce round trips
5. **Show word savings** — "Estimated savings: ~170 words (~0.5 pages)"

### Decision Compression

When the user gives rapid-fire decisions like "1. pick best. 2. ok. 3. ok. 4. which fits best?":
- Execute without asking for confirmation on each
- Only stop if a decision requires genuine clarification
- Batch the implementation and present one final "approve for commit?" at the end

---

## Process

### 1. Understand the Request

Parse the ARGUMENTS to understand:
- **Which document** needs editing (file path)
- **What change** is requested (add, expand, reword, remove, tighten)
- **What content** should be added or modified

### 2. Read the Document

Read the full document to understand:
- Overall structure and voice
- Existing patterns and conventions
- Where the target section is located

Quote the current text that will be modified so the user can confirm the target.

### 3. Check Internal Consistency

When editing content that references concepts elsewhere in the document:
1. Grep for related sections
2. Verify the edit matches positions established elsewhere
3. Flag inconsistencies to the user before proposing

### 4. Verify Factual Claims (if applicable)

If the edit introduces verifiable claims about:
- External entities (companies, organizations)
- Statistics or dates
- Technical comparisons
- Historical events

Use WebSearch to verify before proposing. Don't include claims that can't be substantiated.

### 5. Draft the Edit

Write the proposed new text, ensuring:
- **Voice match**: Preserve the document's existing tone and style
- **Technical accuracy**: Don't introduce claims that aren't true
- **Format correctness**: Proper syntax for the document type

Present the edit as a before/after comparison.

### 6. User Approval (Edit)

**STOP and wait for explicit user approval** before making the edit. The user may want to:
- Adjust wording
- Change emphasis
- Reject and try another direction

Do NOT proceed until user confirms.

### 7. Citation Management (Academic Documents)

For LaTeX or academic documents with citations:

If the edit includes verified external claims, ask:
> "This edit includes verified factual claims. Add citations? (yes/no)"

**STOP and wait for user response.**

If approved:
1. Add citation markers in the text (`\cite{key}` for LaTeX, `[^key]` for Markdown)
2. Add corresponding bibliography entries
3. Order entries by first appearance in document
4. Update bibliography count if needed

### 8. Apply the Edit

Once approved:
- Make the surgical edit using the Edit tool
- Add citations if applicable
- Do NOT change unrelated sections

### 9. Build (if applicable)

For document types with build steps:

**LaTeX:**
```bash
cd <doc-directory> && pdflatex <file>.tex && pdflatex <file>.tex
```
(Run twice for TOC and references)

**Sphinx/RST:**
```bash
cd docs && make html
```

**AsciiDoc:**
```bash
asciidoctor <file>.adoc
```

Inform the user:
- Build completed (or any errors)
- Where to find output
- Ask if they approve for commit

### 10. User Approval (Build Output)

**STOP and wait for explicit approval.** The user may want to:
- Review the rendered output
- Request additional changes
- Approve for commit

### 11. Commit and Push

Once approved:

1. Stage changed files:
```bash
git add <document-files>
```

2. Create commit with descriptive message:
```bash
git commit -m "Update <document>: <brief description>"
```

3. Push to remote:
```bash
git push
```

4. Confirm completion to user.

---

## Decision Points Summary

**ALWAYS STOP and wait for user response at:**
- Edit approval (before applying any change)
- Citation decision (if factual claims added)
- Build output approval (for compiled documents)
- Before reordering or major restructuring
- Any time multiple valid options exist

**Present options clearly** when decisions are needed. Do not assume user intent.

---

## Rationale Gap Analysis (Optional)

After accuracy edits, a document may still lack compelling "WHY" reasoning. This optional pass identifies and fills rationale gaps.

### When to Use

- Major document updates
- When readers frequently ask "why was X designed this way?"
- When sections list capabilities without motivation

### Process

1. **Identify Rationale Gaps**
   - Compare sections with strong rationale vs those that just list features
   - Look for: features without motivation, decisions without justification, "what" without "why"

2. **Present Multi-Option Proposals**
   - For each gap, propose 2-3 framing options
   - Each option should match the document's existing tone
   - Flag options requiring research

   Example:
   ```
   Gap: Rate limiting configuration
   Current: "Rate limits are configurable per endpoint"
   Missing: Why configurability matters, what trade-offs exist

   Option A: Security framing - emphasizes abuse prevention
   Option B: Flexibility framing - emphasizes different use cases
   Option C: Operations framing - emphasizes production tuning
   ```

3. **Core Principle Alignment**
   - Ensure rationale reinforces the project's design ethos
   - Avoid rationale that contradicts stated principles

4. **User Selection**
   - User selects preferred option per gap
   - Apply only after explicit approval

---

## Citation Audit (Academic/Reference Documents)

For documents with citations (LaTeX, academic markdown), perform citation audits when:
- Any citations are added or modified
- User explicitly requests audit
- Document hasn't been audited recently

### Audit Steps

1. **Extract all citation keys** from document body with line numbers
2. **Extract all bibliography entries**
3. **Verify mapping:**
   - Every citation must have a bibliography entry
   - Report orphaned citations (cited but no entry)
   - Report unused entries (entry but never cited)
4. **Check bibliography order:** Should be ordered by first citation appearance
5. **Validate references:** URLs properly formatted, entries complete

### Output Format

```
Citations by first appearance:
| # | Key | First cited (line) | Has entry | Status |
|---|-----|-------------------|-----------|--------|
| 1 | smith2024 | 45 | Yes | OK |
| 2 | jones2023 | 67 | No | MISSING |

Issues found:
- [ ] Missing bibliography entry for 'jones2023'
- [ ] Unused entry 'old_reference'
- [ ] Bibliography out of order
```

---

## Structural Ambiguity Resolution

When uncertain whether something is a bug or intentional design:

1. **Check git history first** before proposing fixes:
   ```bash
   git log --oneline -- <file> | head -10
   git show <commit>:<file> | grep -A 10 "<section>"
   ```

2. **Look for the original pattern** — was it intentionally structured this way?

3. **Don't assume drift is a bug** — sometimes the original structure was deliberate

4. **Ask the user about intent** if git history doesn't clarify

This prevents "fixing" intentional design decisions.

---

## Examples

**Example 1: Simple edit**
```
/doc README.md Add a section about installation requirements
```

**Example 2: Refinement request**
```
/doc docs/whitepaper.tex The introduction feels weak, brainstorm improvements
```
(Triggers option generation, iterative refinement)

**Example 3: Tightening**
```
/doc docs/design.md This is too long, tighten repetitive sections
```
(Triggers repetition audit table, prioritized cuts)

**Example 4: Voice/tone adjustment**
```
/doc README.md Make the tone more professional
```
(Triggers full document read, voice analysis, proposed revisions)

**Example 5: Rapid iteration**
```
/doc emh/philosophy.md Brainstorm a better opening paragraph
```
(Triggers 5-6 diverse options, fusion on request)
