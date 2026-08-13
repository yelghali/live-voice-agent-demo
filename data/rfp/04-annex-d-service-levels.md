# RFP-2026-014 — Annex D: Service Levels and Remedies

## D.1 Availability

| Service | Monthly target | Measurement |
|---------|---------------|-------------|
| Inbound voice | 99.95% | Successful call setup / total attempts |
| Conversational AI | 99.50% | Successful session start / total attempts |
| Analytics dashboards | 99.00% | Synthetic probe every 60 seconds |

Availability excludes agreed maintenance windows notified 10 working days in
advance, capped at 4 hours per month between 01:00 and 05:00 CET on a Sunday.

## D.2 Performance

| ID | Metric | Target |
|----|--------|--------|
| P-01 | Voice call setup time | < 2.0 s at 95th percentile |
| P-02 | Conversational AI first spoken response | < 1.5 s at 95th percentile |
| P-03 | Conversational AI turn latency after user stops speaking | < 1.2 s at 95th percentile |
| P-04 | Speech recognition word error rate, English | < 8% on the authority's test set |
| P-05 | Speech recognition word error rate, French and Arabic | < 12% on the authority's test set |
| P-06 | Transcript availability after call end | < 5 minutes |
| P-07 | Barge-in responsiveness (agent stops speaking) | < 300 ms |

> P-02, P-03 and P-07 will be validated during the proof-of-concept stage using the
> authority's own recorded call samples. Suppliers should note that these targets
> assume a real-time speech-to-speech architecture; cascaded architectures that chain
> separate recognition, generation, and synthesis steps have historically struggled
> to meet P-03.

## D.3 Support response

| Severity | Definition | Response | Resolution target |
|----------|-----------|----------|-------------------|
| Sev 1 | Total loss of inbound voice | 15 minutes | 4 hours |
| Sev 2 | Degraded service affecting > 25% of agents | 30 minutes | 8 hours |
| Sev 3 | Single-user or cosmetic issue | 4 business hours | 10 business days |

Support must be available 24×7 for Sev 1 and Sev 2 in English and French.

## D.4 Service credits

Credits are calculated against the monthly charge for the affected lot.

| Availability achieved | Credit |
|----------------------|--------|
| 99.90% – 99.95% | 5% |
| 99.00% – 99.90% | 10% |
| 95.00% – 99.00% | 20% |
| Below 95.00% | 30% |

Credits are capped at 30% of the monthly charge for the affected lot. Three
consecutive months below target constitutes a material breach and gives the
authority a right to terminate for cause with 30 days' notice.

## D.5 Exit and transition out

The supplier must provide, at no additional charge:

- Full export of call recordings, transcripts, and configuration in open formats.
- Up to 90 days of parallel running with an incoming supplier.
- A documented exit plan, refreshed annually.
