# Copilot instructions for `wayback-recovery`

## Project context
- This repository contains tooling to recover articles from the Wayback Machine.
- Keep solutions focused on reliable content recovery and simple CLI-oriented workflows.

## Coding guidelines
- Prefer small, surgical changes that keep existing behavior intact.
- Use Python standard library features where possible before adding dependencies.
- Keep modules and functions focused; avoid broad refactors unless explicitly requested.
- Preserve backward compatibility for command-line arguments and output formats.

## Validation expectations
- Run the narrowest relevant checks/tests for changed code.
- If no test harness exists for an area, add focused tests only when the repository already uses a test framework.
- Do not modify unrelated files to satisfy linting or formatting.

## Documentation
- Update README or inline docs when behavior, flags, or expected usage changes.
