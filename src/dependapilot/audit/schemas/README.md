# Vendored schemas

## `dependabot-2.0.json`

- **Source:** <https://json.schemastore.org/dependabot-2.0.json>
- **Upstream:** [SchemaStore/schemastore](https://github.com/SchemaStore/schemastore),
  `src/schemas/json/dependabot-2.0.json` (Apache-2.0)
- **Retrieved:** 2026-08-29
- **SHA-256:** `46255f692a8d661325e9d044b50f4b687aff68633b716420b64e460fb212b471`
- **Draft:** JSON Schema draft-07

Vendored rather than fetched at audit time so an audit is reproducible and works
offline: the same `dependabot.yml` must produce the same findings regardless of when
it runs or whether schemastore.org is reachable.

The file is stored **byte-identical to upstream** so it can be diffed against the
source directly. To refresh it:

```sh
curl -sSL -o src/dependapilot/audit/schemas/dependabot-2.0.json \
    https://json.schemastore.org/dependabot-2.0.json
shasum -a 256 src/dependapilot/audit/schemas/dependabot-2.0.json  # update above
```

Then re-run `just check`: `tests/audit/test_schema.py` pins the ecosystem enum and the
`cooldown` shape that the semantic checks in `dependapilot.audit.engine` depend on, so
a breaking upstream change fails loudly instead of silently weakening the audit.
