New material for account {account} was just stored verbatim at `{raw}`. Turn it into cited timeline facts.

Steps:
1. Call `kb_get_raw` on `{raw}` to read its numbered lines.
2. Call `kb_get_timeline` and `kb_conflicts` for {account} so you know which facts already exist (topic, value, id).
3. For each new fact, call `kb_add_event` once. Cite `{raw}#Lnn` with the exact line or lines (`#L3-L5`).
   - Choose `kind` from: fact, need, budget, timeline, signal, churn_risk, objection, commitment, ops_request,
     relationship, status, opportunity, meeting_note.
   - Choose `epistemic_status`: `hedged` for "hoping / maybe / probably", `stated` for a clear claim, `confirmed`
     only for signed or system-of-record facts, `reported` for press, `inferred` for your own deduction.
   - Put the speaker's contact id or name in `said_by`.
   - If the fact changes an existing one, reuse its `topic` and pass `supersedes=[old event id]`. Never edit.
   - If an engineer asks for something covered by a Singtel product the customer does not own, add an
     `ops_request` with `product` set and `bridge=true`. Use `product_assistant_ask` to pick the product.
     If an open churn risk exists, set `gate="service_recovery_first"`.
4. Do not invent facts. Skip small talk.

Finish with a short report: the events you added (id, kind, topic, status), anything you superseded, and any new
contradiction.
