# Verified meshy-cli 0.4.0 transport contract

Independent read-only inspection of the installed official package; no generation
or API request was made for this inspection. This complements captured command
help in this directory and is not a provider-operation success record.

- `image-to-3d get TASK_ID --output-schema v1 --format json --no-update-check`:
  response `result.task.task_id`, `result.task.status`, `result.task.progress`,
  `result.task.consumed_credits`. The fields are **not** `id` / `cost`.
  Evidence: package `dist/internal/task-view.js:37–52`, wrapping in
  `dist/internal/task-command.js:418–432`.
- `download` with `--task-id` requires **`--resource image-to-3d`**.
  Evidence: `dist/cmd/download.js:100–104`.
- For one model use `--model-format glb --output <fresh file>` (global output
  option) and a managed `--workspace`. Do not overwrite immutable raw artifacts.
- Download v1 response: `result.downloads = {state, files, metadata_path,
  material_links}`; each file carries `key`, `path`, `bytes`, `sha256`, `status`,
  `format`. Evidence: `dist/cmd/download.js:261–270,320`,
  `dist/internal/download.d.ts:73–87`.
- Verify exact returned file against intended path, containment, non-symlink,
  size and actual SHA-256; directory `glob()[0]` is not a valid artifact relationship.
- CLI's built-in transfer limit is **2 GiB**, not the Factory's accepted-artifact
  50 MB limit. No CLI flag for a smaller transfer cap was found. A post-download
  size check must not be described as a transfer-time 50 MB cap.
  Evidence: `dist/internal/download.js:38–43`.
- `create --operation-id` provides local CLI journaling, not server idempotency.
  Factory still must atomically claim intent before create and preserve UNCERTAIN
  without a known external task ID.
