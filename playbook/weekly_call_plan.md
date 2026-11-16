Who should I call this week, and why?

Steps:
1. Call `insight_who_to_call`.
2. For the top 3 accounts, call `insight_ops_bridge` and `kb_get_account` to confirm the reasons.
3. Check `kb_weekly_signals` for anything new this week.

Answer with a ranked list of up to 5 accounts. For each one give:
- the reason in one plain sentence (ops request for a product they lack, buying signal, renewal, churn risk);
- the person to call and their role (consent granted, never do-not-contact);
- what to say first, respecting gates (`service_recovery_first` means fix service before pitching);
- citations as `raw/...#Lnn`.
