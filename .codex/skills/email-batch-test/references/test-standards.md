# Batch Acceptance Standards

## P0

P0 is any `included` truth document without one unique, lineage-backed final artifact.

Gate: `P0 = 0`.

## P1

P1 is any wrong category, document type, invoice field, date, amount, seller, purchaser relation, or other truth-defined semantic classification.

Gate: `P1 = 0`.

## P2

P2 is any required hotel invoice/folio or ride invoice/itinerary pair that is unmatched, ambiguously matched, placed in the wrong family, or not archived with adjacent pair naming.

Gate: `P2 = 0`.

## Manual Review

Any included truth row routed to manual review is not a successful automated result.

Gate: `manual = 0`.

## Authority

All four counts must come from a fresh strict audit over finalized run evidence. `audit_authority.authoritative` and `BatchValidator.validate(...)` must both pass. A missing file, incomplete lineage, duplicate artifact assignment, stale audit, or non-final manifest is a failed gate rather than an uncertain result.
