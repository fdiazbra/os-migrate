#!/usr/bin/env python3
"""POC: create a new volume and unmanage it using OpenStack SDK.

No CLI parameters. Edit CONFIG values below if needed.
"""

import json
import sys
import time
import uuid

import openstack

CONFIG = {
    # Cloud in clouds.yaml
    "cloud": "dstdevst",
    # New volume settings
    "size": 2,
    "name_prefix": "poc-unmanage",
    "volume_type": None,
    "availability_zone": "nova",
    # Discovery / waits
    "timeout": 300,
    # Context file consumed by cinder_manage_sdk.py
    "output_json": "/tmp/cinder_poc_ctx.json",
}


def _to_dict(resource):
    if hasattr(resource, "to_dict"):
        return resource.to_dict()
    return dict(resource)


def _get_host(volume_obj):
    host = getattr(volume_obj, "host", None)
    if host:
        return host
    data = _to_dict(volume_obj)
    return data.get("os-vol-host-attr:host") or data.get("host")


def _guess_source_name(volume_id):
    # Common for DevStack/LVM backends
    return "volume-{0}".format(volume_id)


def _find_source_name(conn, host, original_volume_id, timeout):
    start = time.time()
    while (time.time() - start) < timeout:
        for mv in conn.block_storage.manageable_volumes(details=True, host=host):
            ref = mv.get("reference") or {}
            source_name = ref.get("source-name")
            if not source_name:
                continue
            if original_volume_id in source_name:
                return source_name
        time.sleep(2)
    return None


def main():
    conn = openstack.connect(cloud=CONFIG["cloud"])

    unique = str(uuid.uuid4())[:8]
    volume_name = "{0}-{1}".format(CONFIG["name_prefix"], unique)

    create_kwargs = {
        "name": volume_name,
        "size": CONFIG["size"],
        "availability_zone": CONFIG["availability_zone"],
    }
    if CONFIG["volume_type"]:
        create_kwargs["volume_type"] = CONFIG["volume_type"]

    print("[1/4] Creating volume:", volume_name)
    vol = conn.block_storage.create_volume(**create_kwargs)

    print("[2/4] Waiting for 'available'...")
    vol = conn.block_storage.wait_for_status(
        vol,
        status="available",
        failures=["error"],
        interval=5,
        wait=CONFIG["timeout"],
    )

    host = _get_host(vol)
    volume_type = getattr(vol, "volume_type", None) or CONFIG["volume_type"]
    if not host:
        print("ERROR: could not determine host from volume.", file=sys.stderr)
        sys.exit(2)

    print("[3/4] Unmanaging volume:", vol.id)
    conn.block_storage.unmanage_volume(vol)

    print("[4/4] Discovering source-name in manageable volumes...")
    source_name = _find_source_name(conn, host, vol.id, CONFIG["timeout"])
    if not source_name:
        source_name = _guess_source_name(vol.id)
        print(
            "WARNING: source-name not auto-detected; using guess:",
            source_name,
            file=sys.stderr,
        )

    result = {
        "cloud": CONFIG["cloud"],
        "managed_volume_id_before_unmanage": vol.id,
        "host": host,
        "source_name": source_name,
        "volume_type": volume_type,
        "availability_zone": CONFIG["availability_zone"],
        "suggested_new_name": "re-managed-{0}".format(unique),
    }

    print(json.dumps(result, indent=2, sort_keys=True))

    with open(CONFIG["output_json"], "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, sort_keys=True)
        f.write("\n")
    print("Context written to:", CONFIG["output_json"])


if __name__ == "__main__":
    main()
