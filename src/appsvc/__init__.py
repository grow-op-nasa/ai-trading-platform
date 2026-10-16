"""Application services for the local UI -- src/appsvc (Sprint 19, ADR-0054).

A thin, Streamlit-free layer between the dashboard pages and the platform's existing services (paper runtime,
strategy registry, monitoring store, alert outbox, experiment registry). It owns no business logic and no storage:
it opens the same persisted files the CLI uses, returns plain data, and exposes exactly three kinds of operation:

* read-only inspection (never creates a file, never calls a provider, never mutates);
* explicit, named, side-effecting operations (initialise a paper account, run ONE paper cycle, dispatch/retry alerts,
  save a paper-run profile) -- each a single call into the authoritative service;
* nothing else. There is no live-broker path, no shell execution, no AI call, no strategy promotion here.

Importing this package starts nothing.
"""
