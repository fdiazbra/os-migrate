# Task Deliverable: Cinder Manage/Unmanage for os-migrate

## Scope
Research of Cinder Block Storage API `manage` / `unmanage` operations focused on:
- Supported parameters
- Edge cases
- Known limitations/caveats
- Relevance for os-migrate integration

## 1. API Operations (https://docs.openstack.org/api-ref/block-storage/v3/#volume-manage-extension-manageable-volumes)

### Manage (adopt existing backend volume)
- Modern endpoint (microversion >= 3.8):
  - `POST /v3/{project_id}/manageable_volumes`
  - Body: `{"manage": {...}}`
- Legacy endpoint (microversion < 3.8): (https://docs.openstack.org/api-ref/block-storage/api_microversion_history.html#id8 )
  - `POST /v3/{project_id}/os-volume-manage`
  - Body: `{"volume": {...}}`

### Unmanage (remove from Cinder control, keep backend data)
- Endpoint:
  - `POST /v3/{project_id}/volumes/{volume_id}/action`
  - Body: `{"os-unmanage": null}`

### List manageable volumes
- `GET /v3/{project_id}/manageable_volumes`
- `GET /v3/{project_id}/manageable_volumes/detail`

## 2. Supported Parameters

### Manage request
- `ref.source-name`: backend-native identifier of the storage object

### Manage optional (most relevant)
- `host`: target backend host (important in multi-backend)
- `name`
- `description`
- `volume_type`
- `availability_zone`
- `bootable`
- `metadata`
- `cluster` / `cluster_name` (microversion/policy dependent)


### Unmanage
- No functional parameters beyond volume ID in URL

## 3. Edge Cases

Real edge cases observed in OSP16/DevStack-style environments and reproducible with API/SDK:

1. `manageable_volumes` shows candidate but `safe_to_manage=false`
  - Symptom: volume listed, but cannot be managed.
  - Where to validate: `GET /v3/{project_id}/manageable_volumes/detail`.
  - Field to inspect: `reason_not_safe`.
  - Typical causes: already managed, currently in-use, or backend-specific restriction.

2. Wrong `source-name` format for backend
  - Symptom: manage fails with `400 Bad Request` or `404 Not Found`.
  - Repro: use a Ceph-style `rbd:<name>` on an LVM backend (or vice versa).
  - Mitigation: always take `ref.source-name` from `manageable_volumes/detail` instead of guessing.

3. Wrong `host` in multi-backend deployment
  - Symptom: manage fails with backend lookup/placement error.
  - Repro: pass a valid `source-name` with a host from another backend pool.
  - Mitigation: use exact host from `openstack volume service list` and align with the backend that exposes that `source-name`.

4. Unmanage on attached volume
  - Symptom: action rejected (`409 Conflict`/`412 Precondition Failed`, policy/backend dependent).
  - Repro: call unmanage while volume has attachments.
  - Mitigation: detach first, wait for `available`, then unmanage.

5. Race condition immediately after unmanage
  - Symptom: volume not yet visible in `manageable_volumes` right after unmanage.
  - Repro: query list immediately after unmanage.
  - Mitigation: poll with retries/backoff until `source-name` appears.

6. Policy/RBAC allows list but denies manage/unmanage
  - Symptom: `403 Forbidden` on manage/unmanage while list works.
  - Repro: run with non-admin role in hardened policy environments.
  - Mitigation: execute with role/policy that allows volume administrative actions.

7. Microversion mismatch (modern vs legacy manage endpoint/body)
  - Symptom: `404` or request schema error when using wrong endpoint/payload format.
  - Repro: send modern `{"manage": ...}` body to legacy endpoint or vice versa.
  - Mitigation: pin/confirm microversion and endpoint style; in SDK prefer `manage_volume()` to abstract this.

## 4. Known Limitations / Caveats

- Driver behavior is backend-specific; not all drivers support full manage/unmanage paths
- Metadata, snapshot chains, and replication context are not automatically preserved end-to-end by a pure unmanage/manage cycle
- API shape differs by microversion (modern vs legacy endpoint/body)
- OSP16 environments frequently use legacy-style manage endpoint/body
- Operational race conditions:
  - Immediately querying manageable list after unmanage may need retry/polling

## 5. os-migrate Integration Notes

### Recommended integration path
- Use OpenStack SDK first (`conn.block_storage.manage_volume`, `unmanage_volume`)
- Keep REST fallback only for troubleshooting
- Include pre-checks:
  - volume state
  - no attachments
  - host + source-name resolvable
  - `safe_to_manage` true

### Suggested workflow for POC/automation
1. Create test volume in destination cloud
2. Wait for `available`
3. `unmanage`
4. Discover `source-name` via manageable list (with retries)
5. `manage` with explicit `host` (and `volume_type` when needed)
6. Wait for `available`

### SDK implementation proposal (real integration)

Implementation should remain SDK-first and deterministic, with explicit state checks and fallback routing.

#### Core building blocks
- Connection bootstrap per cloud:
  - `openstack.connect(cloud="src")`
  - `openstack.connect(cloud="dst")`
- Pre-check helpers:
  - verify status and attachments
  - validate host/source-name resolvability
  - verify `safe_to_manage` and `reason_not_safe`
- Action helpers:
  - `conn.block_storage.unmanage_volume(volume)`
  - `conn.block_storage.manage_volume(...)`
  - `conn.block_storage.wait_for_status(...)`

#### Suggested service-style flow
1. Build candidate set (source volumes requested by migration plan).
2. Run pre-checks and classify each volume as:
   - `MANAGE_PATH` when conditions are met.
   - `COPY_PATH` when any pre-check fails.
3. Execute `MANAGE_PATH`:
   - ensure detachable state
   - unmanage
   - poll `manageable_volumes/detail` until source-name is visible
   - manage with explicit host (and type/AZ)
   - wait to `available`
4. Execute `COPY_PATH` via conventional create+copy workflow.
5. Persist per-volume outcome for idempotent retries and reporting.

#### Minimal SDK orchestration example

```python
def run_manage_path(conn, volume_id, host, volume_type, az, timeout=300):
    volume = conn.block_storage.get_volume(volume_id)
    if volume.status not in ("available", "error"):
        raise RuntimeError(f"invalid status: {volume.status}")
    if volume.attachments:
        raise RuntimeError("volume has attachments")

    conn.block_storage.unmanage_volume(volume)

    source_name = discover_source_name_with_retry(conn, host, volume_id, timeout)
    managed = conn.block_storage.manage_volume(
        host=host,
        ref={"source-name": source_name},
        name=f"re-managed-{volume_id[:8]}",
        volume_type=volume_type,
        availability_zone=az,
    )

    return conn.block_storage.wait_for_status(
        managed,
        status="available",
        failures=["error"],
        interval=5,
        wait=timeout,
    )
```

#### Integration guardrails
- Always pass explicit `host` for multi-backend deployments.
- Never guess `source-name`; discover from `manageable_volumes/detail`.
- Keep per-volume retry budget and final fallback to `COPY_PATH`.
- Emit structured logs with volume ID, selected path, and failure reason.

### Deliverables produced in this branch
- Research: [cinder-manage-unmanage-research.md](cinder-manage-unmanage-research.md)
- Quick reference: [cinder-manage-unmanage-quick-reference.md](cinder-manage-unmanage-quick-reference.md)
- OSP16 examples: [cinder-manage-osp16-sdk-examples.md](cinder-manage-osp16-sdk-examples.md)
- POC scripts:
  - [scripts/cinder_unmanage_sdk.py](../scripts/cinder_unmanage_sdk.py)
  - [scripts/cinder_manage_sdk.py](../scripts/cinder_manage_sdk.py)

## 6. References / Evidence Sources

### Official OpenStack API and Project Docs
- Cinder Block Storage API v3 index:
  - https://docs.openstack.org/api-ref/block-storage/v3/
- Cinder API microversion history:
  - https://docs.openstack.org/api-ref/block-storage/api_microversion_history.html
- Cinder project documentation (general/admin):
  - https://docs.openstack.org/cinder/latest/

### OpenStack SDK docs (used for integration guidance)
- Block storage proxy documentation (`manage_volume`, `unmanage_volume`, `manageable_volumes`):
  - https://docs.openstack.org/openstacksdk/latest/user/proxies/block_storage.html

### Environment-specific validation evidence (OSP16 / DevStack)
- Real API request/response patterns captured and documented in:
  - [cinder-manage-osp16-sdk-examples.md](cinder-manage-osp16-sdk-examples.md)
- Practical SDK PoC implementation in this branch:
  - [scripts/cinder_unmanage_sdk.py](../scripts/cinder_unmanage_sdk.py)
  - [scripts/cinder_manage_sdk.py](../scripts/cinder_manage_sdk.py)

### Repository-local cross-reference
- Detailed research narrative and parameter tables:
  - [cinder-manage-unmanage-research.md](cinder-manage-unmanage-research.md)
- Condensed operational checklist:
  - [cinder-manage-unmanage-quick-reference.md](cinder-manage-unmanage-quick-reference.md)

## 7. Conclusion
The task is covered: API behavior, parameters, edge cases, and caveats relevant to os-migrate have been researched and documented, with SDK-based PoC scripts to validate behavior in real environments.