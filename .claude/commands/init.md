# Project Initializer

Bootstrap a new project with Holodeck methodology.

This command transforms an empty directory (or existing project) into a Holodeck-powered development environment with specs, documentation, and all slash commands ready to use.

---

## Modes

| Mode | Command | Description |
|------|---------|-------------|
| **New Project** | `/init` | Full interactive setup for new project |
| **Add to Existing** | `/init --existing` | Add Holodeck to existing codebase |
| **From Template** | `/init --template <type>` | Use a project type template |

---

## Interactive Setup (`/init`)

### Step 1: Project Identity

Ask the user:

```
Project Setup
=============

What is your project name?
>

One-line description:
>

Primary programming language?
  1. Go
  2. TypeScript/JavaScript
  3. Python
  4. Rust
  5. Other: ___
>
```

### Step 2: Expertise Profile

```
What expertise should Claude bring to this project?

Examples:
- "Expert distributed systems engineer with deep Kubernetes experience"
- "Senior frontend developer specializing in React and accessibility"
- "Systems programmer with embedded and real-time experience"

Your expertise profile:
>
```

### Step 3: Critical Directives

```
What must NEVER happen in this project?

Common directives:
- No Claude attribution in commits/files
- No console.log in production code
- No secrets in source control
- No breaking API changes without version bump

Your critical directives (one per line, empty line to finish):
>
```

### Step 4: Build & Test Commands

```
How do you build this project?
(Leave empty if not applicable)
>

How do you run tests?
>

How do you run the linter?
>
```

### Step 5: Initial Structure

```
Create initial structure?

[ ] docs/specs/         - Specification documents
[ ] docs/               - General documentation
[ ] CLAUDE.md           - Project guide (required)
[ ] README.md           - Project overview
[ ] .claude/commands/   - Holodeck commands (required)

Which optional items? (comma-separated numbers, or 'all')
>
```

### Step 6: Whitepaper

```
Does this project need a whitepaper?

A whitepaper is useful for:
- Open source projects explaining design philosophy
- Protocol specifications
- Academic or research projects
- Projects seeking community/investor buy-in

Create whitepaper skeleton? (yes/no)
>
```

### Step 7: Initial Spec

```
Would you like to create an initial specification?

This helps establish:
- What you're building
- Core requirements
- Initial scope

Create initial spec? (yes/no)

If yes, what feature/system to spec first?
>
```

---

## File Generation

### CLAUDE.md

Generated from template with user inputs:

```markdown
# {{PROJECT_NAME}} Development Guide

## Claude Expertise Profile

{{EXPERTISE_PROFILE}}

---

## CRITICAL DIRECTIVES

{{CRITICAL_DIRECTIVES}}

---

## Project Overview

**{{PROJECT_NAME}}** - {{PROJECT_DESCRIPTION}}

### Core Components

| Component | Description | Location |
|-----------|-------------|----------|
| *To be filled as project develops* | | |

---

## Build Commands

{{BUILD_COMMANDS}}

---

## Test Commands

{{TEST_COMMANDS}}

---

## Slash Commands

| Command | Description |
|---------|-------------|
| `/warmup` | Initialize session context |
| `/spec` | Create and refine specifications |
| `/generate` | Generate code from specifications |
| `/doc` | Edit documents with collaborative refinement |
| `/reconcile` | Sync specs, code, and documentation |
| `/audit` | Project health check |

---

## Development Workflow

1. Create specification: `/spec create <feature>`
2. Refine until approved: `/spec refine docs/specs/<feature>.md`
3. Generate implementation: `/generate docs/specs/<feature>.md`
4. Document: `/doc README.md` or `/doc docs/<file>.md`
5. Verify consistency: `/reconcile all`
6. Health check: `/audit`
```

### Directory Structure

```bash
mkdir -p docs/specs
mkdir -p docs/audits
mkdir -p .claude/commands
```

### Copy Commands

Copy all Holodeck commands to the new project:

```bash
cp -r <holodeck>/.claude/commands/* .claude/commands/
```

### README.md (if selected)

```markdown
# {{PROJECT_NAME}}

{{PROJECT_DESCRIPTION}}

## Getting Started

*TODO: Add installation and usage instructions*

## Documentation

- [Specifications](docs/specs/) - Feature specifications
- [CLAUDE.md](CLAUDE.md) - Development guide

## Development

This project uses [Holodeck](https://github.com/...) methodology:

```bash
# Start a session
/warmup

# Create a specification
/spec create <feature-name>

# Generate implementation
/generate docs/specs/<feature>.md
```

## License

*TODO: Add license*
```

### Whitepaper (if selected)

Copy whitepaper template:

```bash
cp -r <holodeck>/templates/whitepaper/* docs/whitepaper/
```

Update with project name and description.

---

## Add to Existing Project (`/init --existing`)

### Process

1. **Analyze Current Structure**
   - What language/framework?
   - Where is source code?
   - Where are tests?
   - Existing documentation?

2. **Non-Destructive Setup**
   - Create .claude/commands/ (won't conflict)
   - Create CLAUDE.md (or merge with existing)
   - Create docs/specs/ (or use existing docs/)

3. **Infer Configuration**
   - Detect build commands from package.json, Makefile, etc.
   - Detect test commands
   - Detect existing patterns

4. **Offer to Spec Existing Features**
   ```
   Found existing code without specs:
   - src/auth/ (authentication system)
   - src/api/ (API handlers)
   - src/models/ (data models)

   Create specs for existing code? (yes/no/select)
   ```

---

## Template-Based Init (`/init --template <type>`)

Use predefined templates for common project types:

| Template | Description |
|----------|-------------|
| `go-service` | Go microservice with standard layout |
| `go-cosmos` | Cosmos SDK blockchain |
| `ts-node` | TypeScript Node.js service |
| `ts-react` | TypeScript React application |
| `python-api` | Python FastAPI service |
| `rust-cli` | Rust command-line tool |

Templates include:
- Pre-configured CLAUDE.md
- Appropriate build/test commands
- Starter specs for common features
- Directory structure conventions

---

## Post-Init Actions

After initialization:

```
Project initialized successfully!

Created:
  - CLAUDE.md (project guide)
  - .claude/commands/ (Holodeck commands)
  - docs/specs/ (specification directory)
  - README.md (project overview)

Next steps:
  1. Review and customize CLAUDE.md
  2. Run /warmup to establish context
  3. Create your first spec: /spec create <feature>

Happy building!
```

---

## Examples

**Example 1: New project from scratch**
```
/init
```
(Full interactive setup)

**Example 2: Add Holodeck to existing project**
```
/init --existing
```
(Non-destructive addition)

**Example 3: Use a template**
```
/init --template go-service
```
(Pre-configured for Go microservice)

**Example 4: Quick setup with defaults**
```
/init --quick
```
(Minimal prompts, sensible defaults)
