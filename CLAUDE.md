# CLAUDE.md

## Comment style

- Default to no comments in code you add or edit.
- Only add a comment when the *why* is non-obvious: a hidden constraint, a subtle invariant, or a workaround for a specific bug. Never explain *what* the code does — clear naming should do that.
- Never write multi-paragraph comment blocks or over-explain inline. Match the terseness of the surrounding code.
- Exception: brand-new files may start with a short, genuinely useful module/file-level docstring. This does not license verbose inline comments throughout the rest of the file.
- When editing an existing file, match its existing comment density and style rather than introducing a heavier style than what's already there.

## Project constraints

- Keep `ratsnestpro` and `ratsnest-*` internal IDs stable even when user-facing branding changes.
- The Java control plane owns SaaS identity and Run state; the Python runtime owns Agent execution.
- Never hardcode a board answer or report EDA evidence that was not produced by the toolchain.
- Harness evolution may prepare and evaluate a candidate, but it must not merge, push, or deploy it.
- Prefer narrow unit/static gates; do not start LLM, KiCad, Freerouting, or full Docker for routine edits.
