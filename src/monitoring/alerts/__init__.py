"""Alert events and reliable delivery -- src/monitoring/alerts/ (Sprint 17, ADR-0052).

Kept deliberately light: importing this package starts nothing and sends
nothing. Alert events derive ONLY from persisted deterministic finding
transitions (NEW / ESCALATED / RESOLVED); no LLM is involved anywhere here.
Delivery is a durable outbox processed by an explicit dispatcher.
"""
