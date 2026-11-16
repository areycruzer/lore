# Salesforce setup for Lore

Lore can read and write a real Salesforce org instead of the offline CSV export. These steps use a free
Developer Edition org loaded with the synthetic CRM data (100 accounts, contacts, opportunities, cases, tasks).

## 1. Create a Developer Edition org

1. Sign up at https://developer.salesforce.com/signup and confirm the email.
2. Note your username and My Domain URL (Setup → My Domain), e.g. `https://yourco-dev-ed.develop.my.salesforce.com`.
3. Only needed for the tsmztech backend in `User_Password` mode: get a security token from your avatar →
   Settings → Reset My Security Token. Salesforce emails the token to you.

## 2. Authorize the Salesforce CLI and load the data

```bash
# Installs automatically on first use if missing: npm install -g @salesforce/cli
sf org login web --alias lore --instance-url https://login.salesforce.com

cd v2
uv run python -m lore.sf_load dry-run   # optional: build every payload offline, no network
uv run python -m lore.sf_load all       # deploy fields + permission set, load data, print counts
```

Subcommands: `deploy` (custom fields from `salesforce/force-app` plus the `Lore_Access` permission set, which is
assigned to you), `load` (REST upsert by `Lore_Id__c`), `status` (record counts per object), `all`, and `dry-run`.
Use `--alias <name>` for a different org. Running the load again is safe: records are matched on `Lore_Id__c`.

What gets created:

| Object | Source | Notes |
| --- | --- | --- |
| Product2 | products.csv | |
| Account | accounts.csv | `Type` Customer/Prospect/Other; Parent set in a second pass |
| Contact | contacts.csv | `Lore_Do_Not_Contact__c` = do_not_contact; ReportsTo set in a second pass |
| Opportunity | opportunities.csv | stages mapped onto the standard StageName values |
| Case | support_tickets.csv | Status/Priority/Origin mapped onto the standard picklists |
| Task | activities.csv | only `meeting`, `call`, `email` activities |

Columns without a dedicated field are stored in `Description` as `key: value` lines.

## 3. Configure Lore (`v2/.env`)

```dotenv
LORE_CRM=salesforce
LORE_SF_BACKEND=tsmztech          # or: hosted

# tsmztech backend (open-source Salesforce MCP server)
SALESFORCE_CONNECTION_TYPE=Salesforce_CLI   # reuses the `sf` login above
# or username + password + security token:
# SALESFORCE_CONNECTION_TYPE=User_Password
# SALESFORCE_USERNAME=you@example.com
# SALESFORCE_PASSWORD=...
# SALESFORCE_TOKEN=...
# SALESFORCE_INSTANCE_URL=https://yourco-dev-ed.develop.my.salesforce.com

# hosted backend (official Salesforce Hosted MCP Server)
SF_CLIENT_ID=<External Client App consumer key>
SF_MCP_URL=https://api.salesforce.com/platform/mcp/v1/platform/sobject-all
# SF_CALLBACK_PORT=8787
```

## 4. Optional: the Salesforce Hosted MCP Server (`LORE_SF_BACKEND=hosted`)

1. Setup → search "MCP Servers" (under API Catalog) → enable the SObject All server (`platform/sobject-all`).
2. Setup → External Client App Manager → New External Client App:
   - Enable OAuth. Callback URL: `http://localhost:8787/callback`.
   - OAuth scopes: `mcp_api` (Access Salesforce hosted MCP servers) and `refresh_token` (Perform requests at any
     time).
   - Require PKCE. No client secret is needed for the token exchange: Lore is a public client.
   - Enable "Issue JSON Web Token (JWT)-based access tokens for named users".
   - Copy the Consumer Key into `SF_CLIENT_ID`.
3. Wait up to 30 minutes for the app to propagate, then sign in once:

```bash
uv run python -m lore.connectors.sf_hosted login   # opens the browser, stores tokens in ~/.lore/salesforce (mode 600)
uv run python -m lore.connectors.sf_hosted smoke   # user info + a few Accounts
uv run python -m lore.connectors.sf_hosted tools   # tool and argument names the server exposes
```

Notes on the hosted path:

- The OAuth flow is authorization code + PKCE only. Salesforce has no dynamic client registration, so the
  client id must be pre-supplied.
- Salesforce documents Hosted MCP for Enterprise Edition and above, with Flex Credits. Developer Edition reportedly
  works.
- `mcp-remote` is not supported. Connect directly over streamable HTTP, as Lore does.
- Every tool call is a Salesforce API call and counts against the org's API limits. Writes go one record per call.

## Troubleshooting

- `sf_load` exits with code 2 and prints `sf org login web ...`: the CLI has no valid session for the alias.
- `INVALID_FIELD ... Lore_Id__c`: run `deploy` first, or check that `Lore_Access` is assigned to your user.
- Hosted login says the redirect URI does not match: the callback in the External Client App must be exactly
  `http://localhost:<SF_CALLBACK_PORT>/callback`.
