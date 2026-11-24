#!/usr/bin/env python3
"""Build the NOTORYX Forge offline firmware pack.

Downloads the official firmware files from the Meshtastic and MeshCore GitHub
releases (no forks, no changes), lays them out by system / version / role,
records the size and SHA-256 of every file, and writes manifest.json plus the
firmware licences and source links.

    python3 tools/build_firmware_pack.py --out firmware-pack [--zip NOTORYX-Forge-firmware-pack.zip]

Picks:
  * Meshtastic: the newest release named "... Beta" (Alpha builds are skipped).
  * MeshCore:   the newest companion-, repeater- and room-server- releases
                that share the newest version number.

Uses only the Python standard library. Set GITHUB_TOKEN to avoid API rate limits.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import sys
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

API = "https://api.github.com/repos/"
MT_REPO = "meshtastic/firmware"
MC_REPO = "meshcore-dev/MeshCore"
MC_ROLES = {"companion": "companion", "repeater": "repeater", "room-server": "room_server"}
PACK_FORMAT = 1


def get(url: str, accept: str = "application/vnd.github+json") -> bytes:
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": "notoryx-forge-pack"})
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok and url.startswith("https://api.github.com"):
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def releases(repo: str, pages: int = 3) -> list[dict]:
    out: list[dict] = []
    for page in range(1, pages + 1):
        out += json.loads(get(f"{API}{repo}/releases?per_page=50&page={page}"))
    return [r for r in out if not r.get("draft")]


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def download(url: str, dest: Path) -> Path:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        print(f"  get {dest.name}", flush=True)
        dest.write_bytes(get(url, "application/octet-stream"))
    return dest


# ---------- Meshtastic ----------

def pick_meshtastic(rels: list[dict]) -> dict:
    betas = [r for r in rels if re.search(r"\bBeta\b", r.get("name") or "") and "Revoked" not in (r.get("name") or "")]
    if not betas:
        sys.exit("No Meshtastic Beta release found")
    return max(betas, key=lambda r: r["published_at"])


def meshtastic(out: Path, cache: Path) -> dict:
    rel = pick_meshtastic(releases(MT_REPO))
    tag = rel["tag_name"]
    version = tag.lstrip("v")
    print(f"Meshtastic {version} ({rel['name']})", flush=True)
    base = out / "meshtastic" / version
    targets: list[dict] = []
    for a in rel["assets"]:
        m = re.match(r"firmware-(esp32|esp32c3|esp32c6|esp32s3|nrf52840|rp2040|rp2350|stm32)-.*\.zip$", a["name"])
        if not m:
            continue
        platform = m.group(1)
        z = download(a["browser_download_url"], cache / tag / a["name"])
        with zipfile.ZipFile(z) as zf:
            names = zf.namelist()
            for n in names:
                # The install scripts in the zip are for the command line; Forge flashes itself.
                if n.endswith((".bat", ".sh")) or n.endswith("/"):
                    continue
                dest = base / platform / Path(n).name
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(n) as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
        # One target per board, described by its .mt.json.
        for mt in sorted((base / platform).glob("*.mt.json")):
            meta = json.loads(mt.read_text())
            board = meta.get("platformioTarget") or mt.name.split("-")[1]
            files: dict[str, dict] = {}
            for f in meta.get("files", []):
                p = base / platform / f["name"]
                if p.exists():
                    part = f.get("part_name") or ("factory" if p.name.endswith(".factory.bin") else p.suffix.lstrip("."))
                    files[p.name] = {"file": rel_to(p, out), "part": part, "size": p.stat().st_size, "sha256": sha256(p)}
            for extra, part in ((f"firmware-{board}-{version}.factory.bin", "factory"), (f"firmware-{board}-{version}.uf2", "uf2")):
                p = base / platform / extra
                if p.exists() and extra not in files:
                    files[extra] = {"file": rel_to(p, out), "part": part, "size": p.stat().st_size, "sha256": sha256(p)}
            files = list(files.values())
            if not files:
                continue
            targets.append({
                "board": board,
                "name": meta.get("displayName") or board,
                "platform": platform,
                "mcu": meta.get("mcu") or platform,
                "partitions": meta.get("part", []),
                "files": files,
                "meta": rel_to(mt, out),
                # Lets Forge match a radio (which reports its hardware model) to its firmware.
                "hw_model_slug": meta.get("hwModelSlug"),
                "requires_dfu": bool(meta.get("requiresDfu")),
            })
    # Shared files (for example bleota*.bin) that boards reference by name.
    shared = []
    for p in sorted(base.rglob("*.bin")):
        if not any(p.name in json.dumps(t["files"]) for t in targets):
            shared.append({"file": rel_to(p, out), "size": p.stat().st_size, "sha256": sha256(p)})
    return {
        "system": "meshtastic",
        "channel": "beta",
        "version": version,
        "tag": tag,
        "release_name": rel["name"],
        "published_at": rel["published_at"],
        "release_url": rel["html_url"],
        "source_url": f"https://github.com/{MT_REPO}/tree/{tag}",
        "license": "GPL-3.0",
        "targets": sorted(targets, key=lambda t: (t["platform"], t["board"])),
        "shared": shared,
    }


# ---------- MeshCore ----------

MC_NAME = re.compile(r"^(?P<board>.+?)_(?P<variant>companion_radio_(?:ble|usb|wifi|serial)|companion_radio|[Rr]epeater|[Rr]oom_?[Ss]erver|[a-z_]+)-v(?P<ver>[\d.]+)-(?P<sha>[0-9a-f]+)(?P<merged>-merged)?\.(?P<ext>bin|uf2|zip|hex)$")


def meshcore(out: Path, cache: Path) -> dict:
    rels = releases(MC_REPO)
    picked: dict[str, dict] = {}
    for role in MC_ROLES:
        rs = [r for r in rels if r["tag_name"].startswith(f"{role}-v") and not r.get("prerelease")]
        if rs:
            picked[role] = max(rs, key=lambda r: tuple(int(x) for x in re.findall(r"\d+", r["tag_name"])))
    versions = {r["tag_name"].split("-v")[-1] for r in picked.values()}
    version = max(versions, key=lambda v: tuple(int(x) for x in v.split(".")))
    print(f"MeshCore {version}: " + ", ".join(r["tag_name"] for r in picked.values()), flush=True)
    targets: dict[tuple, dict] = {}
    tags = []
    for role, rel in picked.items():
        tags.append({"role": MC_ROLES[role], "tag": rel["tag_name"], "release_url": rel["html_url"], "published_at": rel["published_at"]})
        for a in rel["assets"]:
            m = MC_NAME.match(a["name"])
            if not m:
                print(f"  skip {a['name']} (unrecognised name)")
                continue
            dest = out / "meshcore" / version / MC_ROLES[role] / a["name"]
            if not dest.exists():
                download(a["browser_download_url"], cache / rel["tag_name"] / a["name"])
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(cache / rel["tag_name"] / a["name"], dest)
            variant = m.group("variant").lower()
            key = (m.group("board"), variant)
            t = targets.setdefault(key, {
                "board": m.group("board"),
                "role": MC_ROLES[role],
                "variant": variant,
                "files": [],
            })
            kind = {"uf2": "uf2", "zip": "dfu", "hex": "hex"}.get(m.group("ext"), "merged" if m.group("merged") else "app")
            t["files"].append({"file": rel_to(dest, out), "part": kind, "size": dest.stat().st_size, "sha256": sha256(dest)})
    return {
        "system": "meshcore",
        "channel": "release",
        "version": version,
        "releases": tags,
        "source_url": f"https://github.com/{MC_REPO}/tree/{picked.get('companion', next(iter(picked.values())))['tag_name']}",
        "license": "MIT",
        "targets": sorted(targets.values(), key=lambda t: (t["role"], t["board"].lower(), t["variant"])),
    }


def rel_to(p: Path, root: Path) -> str:
    return p.relative_to(root).as_posix()


def licences(out: Path, mt: dict, mc: dict) -> None:
    d = out / "LICENSES"
    d.mkdir(parents=True, exist_ok=True)
    (d / "Meshtastic-firmware-GPL-3.0.txt").write_bytes(get(f"https://raw.githubusercontent.com/{MT_REPO}/{mt['tag']}/LICENSE", "text/plain"))
    (d / "MeshCore-MIT.txt").write_bytes(get(f"https://raw.githubusercontent.com/{MC_REPO}/{mc['releases'][0]['tag']}/license.txt", "text/plain"))
    (d / "SOURCE.md").write_text(
        "# Where the source code is\n\n"
        "The firmware in this pack is unchanged from the official releases.\n\n"
        f"- Meshtastic {mt['version']} (GPL-3.0): {mt['source_url']}\n"
        f"  Release: {mt['release_url']}\n"
        f"- MeshCore {mc['version']} (MIT): {mc['source_url']}\n"
        + "".join(f"  Release ({r['role']}): {r['release_url']}\n" for r in mc["releases"])
        + "\nNOTORYX Forge is not made by or affiliated with Meshtastic or MeshCore.\n"
    )


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default="firmware-pack", type=Path)
    ap.add_argument("--cache", default=Path(".firmware-cache"), type=Path)
    ap.add_argument("--zip", type=Path, help="also write the pack as one zip file")
    ap.add_argument("--skip", choices=["meshtastic", "meshcore"], action="append", default=[])
    args = ap.parse_args()
    out: Path = args.out
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    systems = []
    mt = mc = None
    if "meshtastic" not in args.skip:
        mt = meshtastic(out, args.cache)
        systems.append(mt)
    if "meshcore" not in args.skip:
        mc = meshcore(out, args.cache)
        systems.append(mc)
    if mt and mc:
        licences(out, mt, mc)
    manifest = {
        "format": PACK_FORMAT,
        "name": "NOTORYX Forge firmware pack",
        "created": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "id": "-".join(f"{s['system']}-{s['version']}" for s in systems),
        "systems": systems,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    total = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"Pack {manifest['id']}: {sum(len(s['targets']) for s in systems)} targets, {total / 1048576:.0f} MB in {out}")
    if args.zip:
        with zipfile.ZipFile(args.zip, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
            for p in sorted(out.rglob("*")):
                if p.is_file():
                    z.write(p, Path("firmware") / p.relative_to(out))
        print(f"Wrote {args.zip} ({args.zip.stat().st_size / 1048576:.0f} MB)")


if __name__ == "__main__":
    main()
