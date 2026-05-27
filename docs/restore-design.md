# L4 Restore — Design Notes

Companion to `scripts/restore-from-l4.sh` (M2 PR #120, v1.0 roadmap).

## When restore is needed

| Scenario | Trigger | Recovery path |
|---|---|---|
| **Disaster recovery** | MinIO / Postgres host lost (disk failure, datacenter outage) | Restore Postgres from `pg_dump` snapshot (cron-backup.sh) **then** replay L4 for L3 consistency |
| **Accidental DELETE** | Operator wiped an owner's L3 row via SQL | Restore single owner from latest L4 snapshot |
| **Partial corruption** | L3 row's JSONB became invalid after manual edit | Re-import specific snapshot to overwrite (note: ON CONFLICT DO NOTHING — DELETE first) |
| **Owner migration / re-tenant** | Move data of owner X from cluster A → cluster B | Run script with `--target-db postgres://B/` pointing to fresh DB |
| **Audit / forensics** | Need to compare current L3 vs state at date D | Restore into a throwaway DB, diff |

## What lives in L4 vs what does NOT

L4 snapshots are written by `app/services/consolidator.py::_maybe_snapshot()` once per weekly cycle (or when L3 hash changes & interval exceeded). The blob is:

```json
{
  "knowledge": [ ...rows from l3_master_knowledge... ],
  "tools":     [ ...rows from l3_tools_registry...   ],
  "hash":      "sha256...",
  "created_at": "2026-05-27T..."
}
```

| In L4 snapshots | NOT in L4 |
|---|---|
| `l3_master_knowledge` (active rows: `effective_to IS NULL`) | `l1_raw_events` (raw events — pg_dump only) |
| `l3_tools_registry` (active rows) | `l2_daily_buffers` (rolling buffers — pg_dump only) |
| Per-owner partitioning via path prefix | `agent_states` (per-agent checkpoints) |
| `snapshot_hash` for change detection | `rooms`, `room_messages`, `agent_rules` |
| | `user_settings`, `agent_keys`, `accounts`, `sessions` |
| | `l5_audit_log` |
| | pgvector `embedding` columns (must reindex post-restore) |

**Consequence**: full disaster recovery requires *both* `pg_dump` rotated backups (L1/L2/everything-else) **and** L4 snapshots (canonical L3). L4 alone is **not** sufficient.

## Restore granularity

The script supports:

- **Per-owner (default)** — `--owner <uuid>` is required. Multi-tenant safety: never bulk-restore across owners.
- **Per-date** — `--date YYYY-MM-DD` filters MinIO listing to objects with that LastModified date.
- **Per-domain** — `--domain <name>` further narrows to one domain prefix `l4/<owner>/<domain>/`.
- **Per-layer** — `--layer l3` (default for L4) or `--layer all` (warns that L1/L2 won't come from L4).

For sub-day precision (point-in-time recovery), download the specific snapshot UUID manually:

```bash
mc cp local/l4-snapshots/l4/<owner>/<domain>/<snapshot_uuid>.json /tmp/
```

then post-process with `jq` + targeted SQL.

## Test plan (placeholder for CI)

Future `scripts/test-restore.sh`:

1. Spin up disposable Postgres + MinIO via docker-compose (`launch/test-restore.yml`).
2. Seed source DB with synthetic L3 data for 2 owners × 3 domains.
3. Trigger `weekly_consolidate` for each domain → writes L4 snapshots.
4. Wipe `l3_master_knowledge` and `l3_tools_registry` for owner A only.
5. Run `restore-from-l4.sh --owner A --date <today> --target-db ...`.
6. Assert: owner A rows fully restored, owner B rows untouched, counts match pre-wipe baseline.
7. Re-run restore (idempotency): expect 0 new rows inserted (ON CONFLICT DO NOTHING).
8. Run with `--dry-run`: assert no rows inserted, exit 0, prints planned objects.

## Known limitations

1. **No L1/L2 replay** — L4 contains only L3 snapshots. Recovering raw events / daily buffers requires pg_dump or a re-derivation from preserved L1 (which itself is not in L4).
2. **pgvector embeddings lost** — `embedding` column is **not** in the JSON blob. After restore, call the admin reindex endpoint (or run `index_domain_vectors()` per domain) to rebuild HNSW indexes. Otherwise semantic recall returns nothing.
3. **`derived_from_l2_ids` may dangle** — If L2 rows weren't restored, the UUID array references point to non-existent rows. Cosmetic; no FK constraints enforce this.
4. **`effective_to` history collapsed** — Snapshots only include `effective_to IS NULL` rows (active state at snapshot time). Historical / deprecated rows are not preserved.
5. **Snapshot interval** — Default `l4_full_snapshot_interval_weeks` ≥ 1; RPO worst-case = 1 week if no hash-change-triggered intermediate snapshots fired. See RPO section below.
6. **No transactional atomicity across snapshots** — Each downloaded snapshot is applied in its own `--single-transaction` psql call. A multi-snapshot restore that fails mid-way leaves a partial state (idempotent re-run recovers).
7. **Owner UUID assumed valid** — Script does not verify the owner exists in `users` table; restoring for a non-existent owner just creates orphan rows.
8. **Object Lock not relied on** — MinIO bucket is created without WORM. Snapshots can theoretically be tampered/deleted. Enable Object Lock in MinIO Console for production hardening.

## Recovery point objective (RPO) for production

| Data class | Backup mechanism | Frequency | RPO worst-case |
|---|---|---|---|
| L1 raw events | `pg_dump` via cron-backup.sh container | every 6h (configurable) | **6 hours** |
| L2 daily buffers | `pg_dump` | every 6h | **6 hours** |
| L3 master knowledge | L4 snapshot (`_maybe_snapshot`) + `pg_dump` | weekly OR on hash change; pg_dump 6h | **6 hours** (via pg_dump) |
| L3 tools registry | same as L3 knowledge | same | **6 hours** (via pg_dump) |
| MinIO bucket itself | `mc mirror` to /backups/minio/{TS}/ | every 6h with 7d retention | **6 hours** |
| `agent_states`, `rooms`, etc. | `pg_dump` only | every 6h | **6 hours** |

**Recovery time objective (RTO)**: target ≤ 30 min for a single-owner restore (download <100 small JSON objects + ~few thousand INSERTs). Full-cluster restore via `pg_dump` ~10–60 min depending on DB size (~hundreds of MB to single-digit GB historically).

## Operational notes

- Run as `sudo bash` if `psql` connects via local Unix socket as a privileged user; otherwise just `bash`.
- Always start with `--dry-run` to verify the snapshot set matches expectations.
- Use `--force` only after confirming with the owner; the pre-check prints existing row counts.
- After restore, run `POST /admin/reindex-vectors?owner=<uuid>` (or per-domain) to rebuild HNSW indexes for semantic recall — without this, `cognitive_recall` returns empty.
- Verify with the two `GROUP BY domain` queries the script prints in its summary.
- Log the restore event to `l5_audit_log` (`action='restore'`) manually — the script does not write audit rows itself to avoid coupling to the API user model.
