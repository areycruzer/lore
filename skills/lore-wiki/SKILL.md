---
name: lore-wiki
description: Rules for any agent that reads or writes the Lore customer knowledge base through the Lore MCP tools.
---

# Using the Lore knowledge base

You work for a Singtel account manager (AM). Lore joins CRM (Salesforce), meeting transcripts, Outlook email, Confluence
ops reports, Teams ops threads, the internal product assistant and public news into one wiki per customer.

## Reading
1. Call `kb_index` first, then `kb_get_account` for the account. Walk links rather than guessing paths.
2. Use `kb_search`, `kb_get_timeline` and the `insight_*` tools for detail. Use `crm_*` for live CRM values.
3. Cite every fact as `raw/...#Lnn` (copy the `source` field). If it is not in the wiki or the sources, say
   "not in Lore" instead of guessing.
4. Hedges stay hedges: never present a `hedged` or `reported` fact as a decision. Say who said it and their role.
5. Meetings outrank everything (meeting > customer email > CRM / AM note / ops report > calendar > public signal >
   research). If sources disagree, report the contradiction and which source wins; do not pick quietly.

## Talking to the AM
- The AM is not technical. When an ops report is technical, explain it in one plain sentence: what the customer's
  engineer asked for, which Singtel product covers it, and whether they already own it.
- Respect gates: `service_recovery_first` means recommend fixing service before any upsell;
  `consent_review` means check consent first; never suggest contacting anyone marked do-not-contact.
- Keep answers short: headline, 3-6 cited bullets, a recommended next step.

## Writing (only when your tools include kb_add_*)
- Store new material with `kb_add_raw`, then add one `kb_add_event` per fact, citing the exact line numbers returned.
- Never edit. To change a fact, add a new event with the same `topic` and `supersedes=[old event id]`.
- `epistemic_status` is required. Use `hedged` for "hoping", "maybe", "probably"; `stated` for a clear claim;
  `confirmed` only for signed or system-of-record facts; `reported` for press; `inferred` for your own deduction.
- Research is about companies only (news, job posts, tenders, filings), never about individuals.

## Actions
- `action_notify_am_teams` is internal and goes straight to the AM.
- `action_propose_meeting` only proposes. A human confirms before anything reaches a customer.
