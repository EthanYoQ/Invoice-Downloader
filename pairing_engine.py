from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from functools import lru_cache
from typing import Sequence


@dataclass(frozen=True)
class PairingDocument:
    id: str
    role: str
    amount: Decimal | None
    business_date: date | None
    provider: str
    merchant_tokens: frozenset[str]
    source_message_uid: str
    path: str


@dataclass(frozen=True)
class PairingAmbiguity:
    document_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class PairingResult:
    pairs: tuple[tuple[PairingDocument, PairingDocument], ...]
    unmatched_invoices: tuple[PairingDocument, ...]
    unmatched_companions: tuple[PairingDocument, ...]
    ambiguities: tuple[PairingAmbiguity, ...]


_ROLES = {
    "ride": ("ride_invoice", "ride_itinerary"),
    "hotel": ("hotel_invoice", "hotel_folio"),
}
_CENT = Decimal("0.01")
_RIDE_TAX_FACTOR = Decimal("1.03")
_RIDE_TAX_SLACK = Decimal("0.50")


def _compatible(family: str, invoice: PairingDocument, companion: PairingDocument) -> bool:
    invoice_role, companion_role = _ROLES[family]
    if invoice.role != invoice_role or companion.role != companion_role:
        return False
    if invoice.amount is None or companion.amount is None:
        return False

    invoice_provider = invoice.provider.strip().lower()
    companion_provider = companion.provider.strip().lower()
    if invoice_provider and companion_provider and invoice_provider != companion_provider:
        return False

    amount_delta = abs(invoice.amount - companion.amount)
    if family == "ride":
        amount_matches = amount_delta < _CENT or (
            abs(invoice.amount * _RIDE_TAX_FACTOR - companion.amount) < _RIDE_TAX_SLACK
            or abs(companion.amount * _RIDE_TAX_FACTOR - invoice.amount) < _RIDE_TAX_SLACK
        )
        return amount_matches

    if amount_delta > _CENT:
        return False
    if invoice.business_date is not None and companion.business_date is not None:
        return abs((invoice.business_date - companion.business_date).days) <= 3
    return True


def _edge_score(family: str, invoice: PairingDocument, companion: PairingDocument) -> int:
    score = 0
    invoice_provider = invoice.provider.strip().lower()
    companion_provider = companion.provider.strip().lower()
    if invoice_provider and invoice_provider == companion_provider:
        score += 100

    shared_tokens = invoice.merchant_tokens & companion.merchant_tokens
    all_tokens = invoice.merchant_tokens | companion.merchant_tokens
    if shared_tokens and all_tokens:
        score += round(40 * len(shared_tokens) / len(all_tokens))

    if invoice.source_message_uid and invoice.source_message_uid == companion.source_message_uid:
        score += 60

    if invoice.amount is not None and companion.amount is not None:
        score += 30 if abs(invoice.amount - companion.amount) < _CENT else 10

    if invoice.business_date is not None and companion.business_date is not None:
        days = abs((invoice.business_date - companion.business_date).days)
        if family == "hotel":
            score += 20 - (days * 4)
        else:
            score += max(0, 10 - min(days, 10))
    return score


def _connected_components(
    invoice_count: int,
    companion_count: int,
    edges: dict[tuple[int, int], int],
) -> list[tuple[tuple[int, ...], tuple[int, ...]]]:
    neighbors: dict[tuple[str, int], set[tuple[str, int]]] = {}
    for invoice_index, companion_index in edges:
        invoice_node = ("invoice", invoice_index)
        companion_node = ("companion", companion_index)
        neighbors.setdefault(invoice_node, set()).add(companion_node)
        neighbors.setdefault(companion_node, set()).add(invoice_node)

    components = []
    unseen = set(neighbors)
    while unseen:
        pending = [min(unseen)]
        component_nodes = set()
        while pending:
            node = pending.pop()
            if node in component_nodes:
                continue
            component_nodes.add(node)
            unseen.discard(node)
            pending.extend(neighbors[node] - component_nodes)
        component_invoices = tuple(sorted(index for kind, index in component_nodes if kind == "invoice"))
        component_companions = tuple(sorted(index for kind, index in component_nodes if kind == "companion"))
        components.append((component_invoices, component_companions))

    components.sort(key=lambda component: (component[0], component[1]))
    return components


def _optimal_assignments(
    invoice_indices: tuple[int, ...],
    companion_indices: tuple[int, ...],
    edges: dict[tuple[int, int], int],
) -> tuple[tuple[tuple[int, int], ...], ...]:
    companion_bits = {index: bit for bit, index in enumerate(companion_indices)}

    @lru_cache(maxsize=None)
    def solve(position: int, used_mask: int):
        if position == len(invoice_indices):
            return (0, 0), ((),)

        invoice_index = invoice_indices[position]
        choices = [(None, solve(position + 1, used_mask))]
        for companion_index in companion_indices:
            edge = (invoice_index, companion_index)
            bit = 1 << companion_bits[companion_index]
            if edge not in edges or used_mask & bit:
                continue
            choices.append((companion_index, solve(position + 1, used_mask | bit)))

        best_objective = (-1, -1)
        best_assignments: list[tuple[tuple[int, int], ...]] = []
        for companion_index, (child_objective, child_assignments) in choices:
            pair_count, total_score = child_objective
            if companion_index is not None:
                pair_count += 1
                total_score += edges[(invoice_index, companion_index)]
            objective = (pair_count, total_score)
            if objective < best_objective:
                continue
            if objective > best_objective:
                best_objective = objective
                best_assignments = []
            for assignment in child_assignments:
                if len(best_assignments) == 2:
                    break
                candidate = (
                    assignment
                    if companion_index is None
                    else ((invoice_index, companion_index),) + assignment
                )
                if candidate not in best_assignments:
                    best_assignments.append(candidate)
        return best_objective, tuple(best_assignments)

    return solve(0, 0)[1]


def pair_documents(
    family: str,
    invoices: Sequence[PairingDocument],
    companions: Sequence[PairingDocument],
) -> PairingResult:
    if family not in _ROLES:
        raise ValueError(f"Unsupported pairing family: {family}")

    sorted_invoices = tuple(sorted(invoices, key=lambda item: item.id))
    sorted_companions = tuple(sorted(companions, key=lambda item: item.id))
    edges = {
        (invoice_index, companion_index): _edge_score(family, invoice, companion)
        for invoice_index, invoice in enumerate(sorted_invoices)
        for companion_index, companion in enumerate(sorted_companions)
        if _compatible(family, invoice, companion)
    }

    accepted_edges: set[tuple[int, int]] = set()
    ambiguities = []
    for invoice_indices, companion_indices in _connected_components(
        len(sorted_invoices), len(sorted_companions), edges
    ):
        assignments = _optimal_assignments(invoice_indices, companion_indices, edges)
        if len(assignments) > 1:
            document_ids = tuple(
                sorted(
                    [sorted_invoices[index].id for index in invoice_indices]
                    + [sorted_companions[index].id for index in companion_indices]
                )
            )
            ambiguities.append(PairingAmbiguity(document_ids, "multiple_optimal_pair_memberships"))
            continue
        accepted_edges.update(assignments[0])

    pairs = tuple(
        sorted(
            ((sorted_invoices[invoice_index], sorted_companions[companion_index]) for invoice_index, companion_index in accepted_edges),
            key=lambda pair: (pair[0].id, pair[1].id),
        )
    )
    matched_invoice_ids = {invoice.id for invoice, _ in pairs}
    matched_companion_ids = {companion.id for _, companion in pairs}
    return PairingResult(
        pairs=pairs,
        unmatched_invoices=tuple(item for item in sorted_invoices if item.id not in matched_invoice_ids),
        unmatched_companions=tuple(item for item in sorted_companions if item.id not in matched_companion_ids),
        ambiguities=tuple(sorted(ambiguities, key=lambda item: item.document_ids)),
    )
