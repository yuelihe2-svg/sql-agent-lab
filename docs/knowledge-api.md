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

## POST /api/sources/{datasource_id}/grounding

```json
{ "question": "customer name for each order" }
```

Returns tables, columns, declared foreign keys, and knowledge documents that match the question by name or keyword. The match reasons are included. Schema hits are catalog facts. Knowledge hits keep `id`, `version`, `content_hash`, `provenance`, and `verification` (`user_provided_unverified`). A document is never returned with `treat_as_instruction` true, even when its text looks like an instruction.

The question limit is 1,000 characters. At most 8 tables, 8 columns per table, and 5 knowledge excerpts are returned. An excerpt is at most 400 characters. An empty knowledge library or a question with no name overlap returns empty lists. Results are cached by schema fingerprint, knowledge version, question, and retrieval strategy. A schema or knowledge change misses that cache. This route does not read business rows and does not call the Agent.

Optional `retrieval_strategy` is `keyword` (default), `bm25`, or `hybrid`. Omitting it keeps the deterministic keyword algorithm. `keyword` matches schema names and knowledge keywords. `bm25` uses that same schema match and ranks knowledge with the workspace FTS5 index. `hybrid` adds declared foreign keys that touch one matched table. Schema scores and BM25 scores are separate fields and are not added together. `embedding` is rejected because no embedding model is configured. FTS5 uses the `unicode61` tokenizer: a Chinese sentence without spaces is one token, so this is not Chinese semantic search.

Knowledge create, update, and delete write the same title and body into `knowledge_fts` in the workspace database, with the document version and content hash. The index does not contain business-table rows. A query that cannot be parsed as safe quoted tokens matches nothing. If the index rows drift from the knowledge files, the next BM25 search rebuilds the index from those files.

## Conversation grounding

`POST /api/conversations/{id}/messages` accepts an optional boolean `use_grounding`. Omitting it, or sending `false`, keeps the previous execution path and does not call grounding retrieval. Optional `retrieval_strategy` is read only when grounding is on. Omitting it uses `keyword`.

`true` retrieves the bound data source only, builds a bounded untrusted reference block, and appends that block after the existing role prompt. The Selector, Decomposer, and Refiner prompt text is unchanged. User knowledge stays labeled unverified and cannot override system rules. The saved run records matched names, knowledge ids, versions, hashes, the schema fingerprint, context size, and retrieval latency. It does not record an API cost unless a real price is available. Grounding evidence does not prove that the SQL is correct.

## GET /api/sources/{datasource_id}/knowledge/{id}

Returns one document. A missing file or a file that does not match its hash is an error, not an empty document.

## Optional conversation history

Conversation message requests accept a boolean field named
`use_conversation_context`. It defaults to false. The workspace checkbox
**Use conversation history** applies to the current request.

When enabled, the server builds a separate untrusted history block before
Agent execution using public results from earlier runs in the same conversation
and data source. The current run is excluded. The default budget is 1600 UTF-8
bytes including the current question. The current question is always retained.
Oldest turns are removed first, then historical SQL is removed when necessary.
Failed runs contribute no SQL. ContextPolicy checks the full model input after
the block is attached. Offline checks verify transport and isolation, while
follow-up answer quality requires model evaluation.
