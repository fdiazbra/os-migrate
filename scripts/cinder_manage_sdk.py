#!/usr/bin/env python3
"""POC: manage a backend volume into Cinder using OpenStack SDK.

No CLI parameters. Edit CONFIG values below if needed.
By default it reads context from /tmp/cinder_poc_ctx.json produced by
scripts/cinder_unmanage_sdk.py.
"""

import json
import sys

import openstack

CONFIG = {
    # Preferred: read values from context file created by unmanage script
    "input_json": "/tmp/cinder_poc_ctx.json",
    # Fallback fixed values if input_json is missing or incomplete
    "cloud": "dstdevst",
    "host": None,
    "source_name": None,
    "name": "re-managed-volume",
    "volume_type": None,
    "availability_zone": "nova",
    "description": "Managed via OpenStack SDK",
    "timeout": 300,
}


def _load_context(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    ctx = {}
    try:
        ctx = _load_context(CONFIG["input_json"])
        print("Loaded context from:", CONFIG["input_json"])
    except FileNotFoundError:
        print("WARNING: context file not found, using CONFIG fallbacks")

    cloud = CONFIG["cloud"] or ctx.get("cloud")
    host = CONFIG["host"] or ctx.get("host")
    source_name = CONFIG["source_name"] or ctx.get("source_name")
    name = CONFIG["name"] or ctx.get("suggested_new_name") or "re-managed-volume"
    volume_type = CONFIG["volume_type"] or ctx.get("volume_type")
    availability_zone = CONFIG["availability_zone"] or ctx.get("availability_zone") or "nova"

    missing = [
        key
        for key, value in (
            ("cloud", cloud),
            ("host", host),
            ("source_name", source_name),
            ("name", name),
        )
        if not value
    ]
    if missing:
        print("ERROR: missing required values: {0}".format(", ".join(missing)), file=sys.stderr)
        print("Hint: run scripts/cinder_unmanage_sdk.py first or set CONFIG fallback values.")
        sys.exit(2)

    conn = openstack.connect(cloud=cloud)

    kwargs = {
        "host": host,
        "ref": {"source-name": source_name},
        "name": name,
        "description": CONFIG["description"],
        "availability_zone": availability_zone,
    }
    if volume_type:
        kwargs["volume_type"] = volume_type

    print("[1/2] Managing backend object into Cinder...")
    vol = conn.block_storage.manage_volume(**kwargs)

    print("[2/2] Waiting for 'available'...")
    vol = conn.block_storage.wait_for_status(
        vol,
        status="available",
        failures=["error"],
        interval=5,
        wait=CONFIG["timeout"],
    )

    print("Managed volume:")
    print(json.dumps({"id": vol.id, "name": vol.name, "status": vol.status}, indent=2))


if __name__ == "__main__":
    main()
