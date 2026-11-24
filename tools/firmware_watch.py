#!/usr/bin/env python3
"""Daily check for new Meshtastic or MeshCore firmware.

    python3 tools/firmware_watch.py check        # prints changed=true/false for GitHub Actions
    python3 tools/firmware_watch.py latest PACK_DIR ZIP TAG   # writes firmware/latest.json

`check` compares the newest upstream releases (Meshtastic Beta, MeshCore
release) with firmware/latest.json. `latest` describes a freshly built pack
for NOTORYX Forge, which fetches firmware/latest.json to find new packs.
Standard library only.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import build_firmware_pack as b  # noqa: E402

LATEST = Path("firmware/latest.json")
REPO = "TheLogsAreScreaming/NOTORYX-Forge-Releases"


def upstream() -> dict[str, str]:
    mt = b.pick_meshtastic(b.releases(b.MT_REPO))["tag_name"].lstrip("v")
    rels = b.releases(b.MC_REPO)
    versions = set()
    for role in b.MC_ROLES:
        rs = [r for r in rels if r["tag_name"].startswith(f"{role}-v") and not r.get("prerelease")]
        if rs:
            top = max(rs, key=lambda r: tuple(int(x) for x in re.findall(r"\d+", r["tag_name"])))
            versions.add(top["tag_name"].split("-v")[-1])
    mc = max(versions, key=lambda v: tuple(int(x) for x in v.split(".")))
    return {"meshtastic": mt, "meshcore": mc}


def published() -> dict[str, str]:
    if not LATEST.exists():
        return {}
    return {s["system"]: s["version"] for s in json.loads(LATEST.read_text())["systems"]}


def check() -> None:
    up, have = upstream(), published()
    changed = up != have
    print(f"upstream {up}, published {have}", file=sys.stderr)
    print(f"changed={'true' if changed else 'false'}")


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def latest(pack: Path, zip_path: Path, tag: str) -> None:
    m = json.loads((pack / "manifest.json").read_text())
    out = {
        "format": 1,
        "pack_id": m["id"],
        "created": m["created"],
        "systems": [{"system": s["system"], "version": s["version"], "channel": s.get("channel", "")} for s in m["systems"]],
        "url": f"https://github.com/{REPO}/releases/download/{tag}/{zip_path.name}",
        "size": zip_path.stat().st_size,
        "sha256": sha256(zip_path),
    }
    LATEST.parent.mkdir(parents=True, exist_ok=True)
    LATEST.write_text(json.dumps(out, indent=1) + "\n")
    print(json.dumps(out, indent=1))


if __name__ == "__main__":
    if sys.argv[1:2] == ["check"]:
        check()
    elif sys.argv[1:2] == ["latest"] and len(sys.argv) == 5:
        latest(Path(sys.argv[2]), Path(sys.argv[3]), sys.argv[4])
    else:
        sys.exit(__doc__)
