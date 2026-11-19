# Database profile and business knowledge

These routes exist only when the server is started with `--workspace-db`. Without that flag they return 404. They do not change Selector, Decomposer, or Refiner prompts.

A profile is catalog metadata: table names, column names, SQLite types, declared primary keys, and declared foreign keys. It does not include file paths, row samples, or inferred business meanings. `business_definitions` is `not_inferred`.

Knowledge is optional user text. `verification` is always `user_provided_unverified`. The server stores the bytes beside the workspace file under a generated key. Clients cannot choose that location.

## GET /api/sources/{datasource_id}/profile

Refreshes the profile from the registered SQLite file and saves it.

```json
{
  "datasource_id": "shop",
  "name": "Shop",
  "kind": "sqlite",
  "read_only": true,
  "fingerprint": "<sha256>",
  "extracted_at": "2026-11-18T22:00:00+00:00",
  "tables": [
    {
      "name": "orders",
      "primary_key": ["id"],
      "columns": [{"name": "id", "type": "INTEGER", "primary_key": true}],
      "foreign_keys": []
    }
  ],
  "relationships": [],
  "coverage": {
    "table_count": 1,
    "tables_without_primary_key": [],
    "declared_foreign_key_count": 0,
    "business_definitions": "not_inferred"
  },
  "schema_facts": "catalog",
  "business_definitions": "not_inferred"
}
```

## GET /api/sources/{datasource_id}/knowledge

Returns `{ "knowledge": [ ...documents ] }`.

## POST /api/sources/{datasource_id}/knowledge

```json
{ "title": "Revenue rules", "format": "markdown", "body": "Net collected revenue is a user definition." }
```

`format` is `markdown`, `text`, or `json`. JSON must be an object or array. The body limit is 24,000 characters. `path`, `filename`, and `storage_key` are rejected.

The response adds `id`, `version` (starting at 1), `content_hash`, `provenance` (`user`), and `verification`.

## PUT /api/sources/{datasource_id}/knowledge/{id}

Same body as create. Each successful update increments `version`. The hash changes only when the body changes.

## DELETE /api/sources/{datasource_id}/knowledge/{id}

```json
{ "id": "<id>", "datasource_id": "shop", "deleted": true }
```

## GET /api/sources/{datasource_id}/context

Returns the saved profile and the knowledge documents for that source only. A later database-introduction step can read this object. It must still treat knowledge as unverified user text and must not append it to the current Agent prompt unless that change is explicit.

## GET /api/sources/{datasource_id}/knowledge/{id}

Returns one document. A missing file or a file that does not match its hash is an error, not an empty document.
