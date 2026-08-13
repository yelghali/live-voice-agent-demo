# RFP-2026-014 — Annex C: Pricing Schedule Instructions

All prices in EUR, excluding VAT, fixed for the first 24 months.

## C.1 Pricing model

Suppliers must price all three lots separately using the workbook tabs provided.

| Tab | Lot | Basis |
|-----|-----|-------|
| `Tab-1-Voice` | Lot 1 | Per named agent seat, per month |
| `Tab-2-AI` | Lot 2 | Per resolved self-service interaction |
| `Tab-3-Analytics` | Lot 3 | Per 1,000 minutes transcribed |
| `Tab-4-Transition` | All | One-off, milestone-based |

## C.2 Volume assumptions

Use these figures for the evaluated price. Do not substitute your own.

| Metric | Year 1 | Year 2 | Year 3 | Year 4 |
|--------|--------|--------|--------|--------|
| Named agent seats | 240 | 240 | 220 | 200 |
| Total interactions (millions) | 1.80 | 1.85 | 1.90 | 1.95 |
| Target self-service containment | 15% | 30% | 40% | 45% |
| Minutes transcribed (millions) | 4.2 | 4.3 | 4.4 | 4.5 |

## C.3 Rules

- **C.3.1** Transition costs (Tab 4) must not exceed 15% of the four-year total.
- **C.3.2** Any charge not entered in the workbook is deemed included at zero cost.
- **C.3.3** Price increases after month 24 are capped at the Eurozone HICP annual
  rate, or 3%, whichever is lower.
- **C.3.4** Consumption-based AI charges must be quoted as a single blended rate per
  resolved interaction. Suppliers may not pass through per-token model pricing.
- **C.3.5** The authority will not accept minimum-commit or take-or-pay terms.
- **C.3.6** Overage rates must be stated and may not exceed 120% of the standard rate.

## C.4 Evaluated price formula

Evaluated price is the sum of all four tabs across the four-year base term,
discounted to present value at 3.5% per annum. Extension years are excluded from
the evaluated price but rates must still be provided.

Price score is calculated as:

```
price_score = 30 × (lowest_compliant_evaluated_price / supplier_evaluated_price)
```

## C.5 Common disqualifiers

Based on prior procurements, the following have led to bids being set aside:

- Submitting pricing inside the technical response rather than the workbook.
- Altering the workbook formulas or inserting rows.
- Quoting per-token or per-minute AI pricing instead of per resolved interaction
  (breaches C.3.4).
- Omitting extension-year rates.
