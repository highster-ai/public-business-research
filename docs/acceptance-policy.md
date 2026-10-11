# Acceptance policy

Every completed run reports, for each requested field:

- `found%`: rows whose status is `found`.
- `resolved%`: rows whose status is `found` or `not_public`.
- `failed%`: rows whose status is `not_found` or `ambiguous`.

The default gate fields are `google_rating`, `google_review_count`, and
`public_contact`. Each must have at least 70% resolved coverage for a run to
be approved. Instagram and WhatsApp are warning-only fields. Owner and the
public-evidence DM proxy are informational and never gate approval.

The overall useful-cell ratio is reported across all requested fields. It is
informational in v0.1 and does not override the per-field gate.

Run states:

- `approved`: all gate fields pass and no task exhausted its iteration budget.
- `partial_success`: the run completed, but a gate field failed or at least one
  task exhausted its iteration budget.
- `failed`: a structural, infrastructure, validation, unresolved-error, or
  business-count failure prevented an acceptable completed dataset.
