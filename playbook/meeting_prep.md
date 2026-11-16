Prepare a one-page pre-meeting brief on account {account} for its account manager.

Steps:
1. Call `insight_brief_pack` with account {account}. Then call `kb_get_account` and `insight_account_map` for {account}.
2. If a fact looks stale or a contradiction is open, check it with `kb_get_raw` before you repeat it.
3. Use `crm_get_opportunities` and `crm_get_contacts` for live CRM values. Say whether they came from Salesforce or the export.

Write the brief with these sections, in plain English for a non-technical account manager:
- **Headline**: one sentence on where the account stands today.
- **What changed since the last meeting**: superseded facts, with the old value, the new value and the source of each.
- **Open contradictions**: which source wins and why (meeting > email > CRM / ops > press).
- **What their engineers asked Ops for**: one plain sentence per item, the Singtel product that covers it, and whether they already own it.
- **Open objections and commitments**: who said what, and their role. Keep hedges as hedges.
- **Who to talk to**: champion, economic buyer, blocker. Never suggest a do-not-contact person.
- **What not to pitch yet**: respect `service_recovery_first` and `consent_review` gates.
- **Recommended next step**: one action with an owner and a date.

Cite `raw/...#Lnn` after every fact. If something is not in Lore, write "not in Lore".
