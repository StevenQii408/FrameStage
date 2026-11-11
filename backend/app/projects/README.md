# Project and canvas storage

Each project owns one `canvas.v2` document. The SQLite migration adds `projects`,
`project_canvases`, `project_asset_refs` and nullable ownership columns on the
existing analysis and generation task tables. Existing results, input hashes,
events, files and local browser image Blobs are not rewritten.

## HTTP contract

- `GET /api/projects` returns an array of project summaries. `?deleted=true`
  returns the recycle bin. `limit`/`offset` provide pagination; `q` searches the
  full name index before pagination, treating `%` and `_` literally.
- `POST /api/projects {name}`, `GET/PATCH/DELETE /api/projects/{id}` and
  `POST /api/projects/{id}/restore` manage the lifecycle. Detail includes
  `job_ids` and `generation_ids`, including work not yet saved in a canvas.
  Creation accepts optional `idempotency_key` so two tabs migrating the same
  legacy canvas cannot create competing projects. Same key/name reuses the
  ID; a changed name returns 409.
- `GET /api/projects/{id}/canvas` returns
  `{project_id, revision, schema_version, state, updated_at}`.
- `PUT /api/projects/{id}/canvas {expected_revision, state}` increments the
  revision or returns 409. A failed save never overwrites newer work.
- `POST /api/projects/{id}/import-legacy` accepts
  `{job_ids, generation_ids, asset_ids?, state?, expected_revision?}`. It claims
  only unowned tasks or the same project's tasks, and saves the canvas in one
  transaction. Exact migration replays return the existing revision.
- `POST /api/projects/{id}/assets {asset_ids}` explicitly binds unowned uploaded
  assets. Uploading an image again is required to share another project's image.

Analysis creation accepts multipart `project_id`; generation preflight/create
accept JSON `project_id`; reference uploads accept multipart `project_id`.
All task subroutes, frame images, SSE and generated asset reads accept either
`X-Project-ID` or `?project_id`. Conflicting contexts are rejected. Requests
without a context remain compatible with historical single-user clients.
This is project isolation within the local single-user service, not an
authentication or multi-tenant authorization system.

## Deletion and assets

Delete moves the project to the recycle bin and cancels its queued/running
analysis, generation, event-Agent, perception recovery and style tasks.
Completed results and physical assets remain.
Restoring never resubmits paid tasks. Late worker status updates cannot revive
cancelled work, while late token/cost records can still be persisted.

Assets use content-addressed IDs and many-to-many membership. Deleting one
project cannot delete a reference still used elsewhere. No physical image GC
or irreversible project purge is exposed. Canvas JSON is limited to 4 MB and
stores references, not image bytes.

## Generation inputs

`settings.model_id` is nullable. Provider capabilities expose `models: []`
while disabled; no model IDs or generation results are fabricated.
`reference_images` supports at most eight selected bindings:

```json
{
  "reference_id": "ref-0123456789ab",
  "asset_id": "genasset-<64 lowercase hex>",
  "mention": "图片1",
  "role": "reference"
}
```

Aliases and IDs must be unique, `@图片N` must resolve to a binding, and the
legacy `reference_asset_id` must designate a selected image when bindings are
provided. Video may designate one `first_frame` binding. Actual provider
capabilities can impose a lower reference count. Ownership is kept outside
the immutable request content hash; null model and empty bindings preserve
legacy request hashes.

New video generation requests accept integer durations of 5–15 seconds,
defaulting to 5. Provider/model limits may narrow this range. The capability
protocol reserves an extension ceiling of 30 seconds; declaring a 30-second
model does not enable 30-second requests under the current policy. Source
analysis shot durations are independent and are not rewritten.

Historical attempts remain readable. A lost-response POST with the original
key, project and exact content hash returns the stored attempt without new
work, even for a formerly valid duration. Creating a distinct retry with a
historical fractional/sub-five-second duration returns 409 and requests a
new edited submission. Whole legacy floats such as `5.0` can retry without
rewriting their request or hash. For a read-only replay of an existing whole
float snapshot, the equivalent JavaScript integer wire representation is
accepted because JSON.stringify emits `5` for `5.0`; the stored snapshot and
hash remain the original float, and distinct new requests remain strict.
