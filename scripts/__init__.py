"""Standalone worked examples -- not part of the installable `src`
package's public API.

Deliberately outside `src/` (`ROADMAP.md`, Sprint 6 close-out): a
script here can wire the platform's modules together end to end
without committing the platform to a permanent orchestration layer
(`DECISIONS.md`, ADR-0021 already flags this as deferred work). Import
`from scripts.run_experiment import run_experiment` in tests the same
way any other package is imported -- this `__init__.py` exists only so
that works.
"""
