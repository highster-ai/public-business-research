# Contributing

Contributions should preserve deterministic orchestration, evidence provenance, visible failures, no-outreach behavior, and source-access honesty.

1. Create a focused branch and avoid committing generated runs, browser profiles, `.env`, JSONL, spreadsheets, or lead exports.
2. Add synthetic fixtures; do not contribute scraped personal data or secrets.
3. Run the full suite with ResourceWarnings treated as errors.
4. Describe behavior changes, tests, and any new external-data or provider assumptions in the change.

Changes that weaken validation, hide partial results, automate messaging, require personal-account login, or bypass source restrictions will not be accepted.
