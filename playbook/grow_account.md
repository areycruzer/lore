Find expansion opportunities in account {account}.

Steps:
1. Call `insight_whitespace` and `insight_ops_bridge` for {account}.
2. Call `kb_get_account` and `crm_get_opportunities` for {account} to see what they own and what is already in flight.
3. For each candidate product, ask `product_assistant_ask` how it fits the need, passing account {account}.
4. Call `insight_account_map` for {account} to find who would sponsor each opportunity.

Answer with up to 3 opportunities. For each one give:
- the customer need in plain words and who raised it;
- the Singtel product, with a one-line reason it fits;
- the sponsor to approach;
- any gate that blocks outreach today;
- citations as `raw/...#Lnn`.

Do not propose products the customer already owns or has an open opportunity for.
