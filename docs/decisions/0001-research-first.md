# ADR-0001: Research-first, no premature fork

**Status:** Accepted for planning · **Date:** 2026-11-15

## Context
NeuroWave is a new public Linux music player project with unverified compatibility, performance and licensing requirements.

## Decision
Do not import or fork third-party player source yet. Evaluate CloudMus, YaMusic and a custom Qt6-based approach with measurable tests. Keep a modular reference architecture and select only after discovery.

## Consequences
- Development begins with validated prototypes and a comparison report.
- MIT/GPL/LGPL obligations are reviewed before any copied code.
- API availability, audio fidelity and resource use become release gates rather than assumptions.
