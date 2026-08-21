# Sources and implementation decisions

## Company-confirmed public signals

The project direction was selected from these public pages, read on 2026-08-20:

- [Launcher role](https://jobs.ashbyhq.com/dualentry/28f2277d-a348-499a-9a2f-6d178e4f4cc6):
  zero-to-one products, internal and external AI tools, process simplification, high-priority
  strategic and operational ownership, analytical thinking, high agency, and builder mindset.
- [ERP Rescue Fund](https://www.dualentry.com/blog/6m-rescue-fund-launch): a current public
  initiative addressing customers with remaining legacy ERP subscription commitments.
- [Implementation and migration](https://www.dualentry.com/implementation-and-migration):
  full-history migration, real-data sandboxing, speed to value, integrations, and evidence rather
  than perfect fake-data demonstrations.
- [Frontend Engineer](https://jobs.ashbyhq.com/dualentry/92576bb6-df72-4592-b54c-606952dcf991):
  React, Next.js, JavaScript/TypeScript, scalable web applications, reusable components,
  responsiveness, accessibility, usability, and frequent delivery.
- [Backend Engineer](https://jobs.ashbyhq.com/dualentry/bb7da28b-f93b-42ff-87dd-10fde58bb359):
  Python, PostgreSQL, SQL, schema migrations, robust APIs, AWS, business-critical accounting logic,
  testing, monitoring, migrations, and edge-case ownership.
- [Close Management](https://www.dualentry.com/core-financials/close-management-software):
  status visibility, signoffs, reconciliations, review notes, alerts, and close checklists.

## Portfolio choices, not company claims

- RescueDesk's product scope and name
- FastAPI, SQLAlchemy, Alembic, ReportLab, PyMuPDF, and all version ranges
- Modular-monolith architecture
- Data model, workflow states, roles, formulas, hashes, and export formats
- Optional local extraction model
- Visual palette and implemented frontend behavior
- Every fixture, benchmark, test, and demonstration result

The public backend posting names FastAPI, Flask, and Django as useful experience; that does not prove
which framework DualEntry uses in production. RescueDesk selects FastAPI pragmatically and says so.

## Deliberate adjacency

RescueDesk does not rebuild an ERP, close-management product, or private Rescue Fund decision engine.
It addresses an adjacent operating problem: assembling contract evidence and deterministic scenario
math for human review. No private eligibility rule, customer data, logo, screenshot, proprietary
font, or claim of endorsement is used.

## Visual boundary

After the owner chose the Evidence Workbench direction from three presented options, the UI was
implemented as an independently branded operational tool: dense readable tables, compact status
labels, a synchronized PDF/evidence workspace, visible workflow gates, responsive mobile layouts,
and restrained use of the documented public teal palette. It uses no DualEntry logo, proprietary
screenshot, private copy, customer data, or claim that the visual system reproduces company
software.
