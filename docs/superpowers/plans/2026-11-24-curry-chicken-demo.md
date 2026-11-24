# Curry chicken demo implementation plan

> **For agentic workers:** Use the subagent-driven-development or executing-plans skill. Track and verify each task locally before proposing a release.

**Goal:** Walk through recipe selection, goal adjustment, a confirmed inventory purchase, cooking, and meal logging in the shared demo.

**Architecture:** Keep the existing FastAPI, PostgreSQL, workflow, and calculator. Add structured presentation data and stage-aware demo actions. Keep real model calls disabled throughout local verification.

**Tech stack:** Python 3.12, FastAPI, PostgreSQL, HTML, CSS, JavaScript, Docker, pytest, and Playwright.

**Spec:** User-approved plan in the conversation. Reset starts with no batches or meals. Display curry chicken using the existing recipe and sample ingredient quantities. Set meal goals to 1,000 kcal, 70 g protein, 100 g carbs, and 20 g fat. Adjust four boxes, detect a 160 g chicken shortage, and resolve it by approving a 200 g purchase.

## Implementation and verification

- [x] Replace the demo fixture and verify the starting and adjusted nutrition against the calculator. Keep demo inventory separate from personal data and evaluation fixtures.
- [x] Expose friendly inventory, batch and daily summaries, both target sets, and a stage-aware recipe picker. Verify zero records at reset and natural selection.
- [x] Compute and recheck shortages in the proposal and acceptance paths. Verify rejection, a stock change in another session, and successful purchase approval without creating a different proposal.
- [x] Present readable cooking and preparation details. Clear the accepted proposal after saving. Verify four boxes, 40 g of chicken left, and no repeated acceptance action.
- [x] Resolve natural meal references and show clickable choices for ambiguity. Verify an unresolved or stale choice logs no meal, then verify that one choice deducts one box.
- [x] Run the local HTTP, PostgreSQL and two-window browser checks without model calls. Leave the result available for review before changing the live site.
