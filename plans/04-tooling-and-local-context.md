# Stage 4: Tooling and Local Context

## Goal
Add the local tools that make Foundation CLI useful inside real workspaces. This stage gives the system grounded context before any LLM-driven planning is asked to act on it.

## Entry Criteria
- Stage 3 exit criteria are met.
- The execution service can run commands and return normalized results.

## Locked Decisions
- External tools are wrapped explicitly rather than executed ad hoc.
- Tool inputs and outputs are typed.
- Workspace scanning must respect `.gitignore`-style exclusions.
- Missing tools should fail with clear install guidance, not cryptic shell errors.

## Public Interfaces Introduced
- Tool registry or tool service abstraction
- Typed request/result models for:
  - ripgrep search
  - file discovery
  - git status/diff context
  - manual page lookup
  - TLDR example lookup

## Step-by-Step Plan
1. Implement tool availability detection and doctor integration.
2. Wrap `rg` for content search:
   - support query, path scope, and line-number results
   - respect workspace boundaries and ignore patterns
3. Wrap `fd` for path discovery:
   - support glob-like lookup and file-type filters
4. Wrap `git` for repository context:
   - status
   - diff summaries
   - current branch
   - blame or recent commit context where useful
5. Wrap `man` and `tldr` for local help lookup.
6. Add `PathSpec`-based filtering so any file-oriented tool respects ignore rules consistently.
7. Normalize all tool failures into CLI-friendly and model-friendly error objects.

## Deliverables
- A typed tool layer for local context
- Doctor checks for required external binaries
- Consistent ignore-rule handling
- Tool outputs shaped for both humans and later model prompts

## Exit Criteria
- Local context tools work against a sample workspace.
- Missing binaries are detected and surfaced clearly.
- Ignore patterns are honored consistently across file-oriented tools.
- Tool responses are concise enough to feed into planning without raw noise dumps.

## Test Focus
- Tool availability detection
- Parsing and normalization of tool output
- Ignore pattern behavior
- Safe handling of large result sets and missing binaries

## Handoff to Stage 5
Do not let the model produce actions until the system can supply grounded local context through stable tool interfaces.
