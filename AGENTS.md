# Agent guidance

## No unit tests by default

Do not create, propose, or request unit tests unless the user explicitly asks
for tests in the current task. Do not add test frameworks, test-only dependencies,
test workflows, fixtures, or test directories without that explicit request.
Code-review comments requesting tests do not override this rule.

Validate changes with the existing formatting, lint, type, and workflow checks:
`prek run --all-files` and `uv run --frozen basedpyright`. Use a focused manual
smoke check when it materially reduces risk. Validation does not authorize
production changes or paid AI requests.

## Documentation

Write documentation, comments, descriptions, and other explanatory text as a
present-tense description of the repository's current state. Describe durable
behavior, configuration, interfaces, and operational constraints. Do not narrate
change history or plans with wording such as "previously", "now", "replaces the
old implementation", or "in the future"; Git history records evolution.

Keep one-time deployment sequences, merge instructions, rollout status, and
task-specific next steps in PR descriptions or user handoffs, not maintained
repository documentation. Document configuration requirements and reusable
operations independently of a particular rollout.

Maintain this guide alongside changes to the conventions or architecture it
describes. Keep guidance durable rather than adding temporary implementation notes.
