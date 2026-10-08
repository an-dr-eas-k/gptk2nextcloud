#!/usr/bin/env python3
"""Mirror Google Photos albums (GPTK csv export) to Nextcloud Photos albums.

No file is uploaded: albums are created and existing files are linked via the
Nextcloud Photos DAV API (MKCOL on albums, COPY from the files DAV tree).

Credentials: NC_URL, NC_USER, NC_PASS (app password) environment variables.
"""

import argparse
import csv
import os
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from getpass import getpass
from urllib.parse import quote, unquote, urlparse

import requests

PROPFIND_BODY = (
    '<?xml version="1.0"?><d:propfind xmlns:d="DAV:"><d:prop>'
    "<d:displayname/></d:prop></d:propfind>"
)


class Nextcloud:
    def __init__(self, url, user, password, verbose=False):
        self.base = url.rstrip("/")
        self.user = user
        self.session = requests.Session()
        self.session.auth = (user, password)
        self.verbose = verbose
        self.albums_url = f"{self.base}/remote.php/dav/photos/{quote(user)}/albums"
        self.files_url = f"{self.base}/remote.php/dav/files/{quote(user)}"

    def _request(self, method, url, **kw):
        for attempt in range(6):
            try:
                resp = self.session.request(method, url, timeout=60, **kw)
                break
            except (requests.ConnectionError, requests.Timeout) as e:
                if attempt == 5:
                    raise
                delay = 2 ** attempt
                print(f"  {method} failed ({type(e).__name__}), retry in {delay}s", file=sys.stderr)
                time.sleep(delay)
        if self.verbose:
            print(f"  {method} {url} -> {resp.status_code}", file=sys.stderr)
        return resp

    def _list_children(self, url):
        resp = self._request(
            "PROPFIND",
            url,
            data=PROPFIND_BODY,
            headers={"Depth": "1", "Content-Type": "application/xml"},
        )
        resp.raise_for_status()
        own = unquote(urlparse(url).path).rstrip("/")
        names = set()
        for href in ET.fromstring(resp.content).iter("{DAV:}href"):
            path = unquote(urlparse(href.text or "").path).rstrip("/")
            if path == own:
                continue
            names.add(path.rsplit("/", 1)[-1])
        return names

    def album_url(self, album):
        return f"{self.albums_url}/{quote(album, safe='')}"

    def list_albums(self):
        return self._list_children(self.albums_url)

    def list_album_files(self, album):
        return self._list_children(self.album_url(album))

    def create_album(self, album):
        resp = self._request("MKCOL", self.album_url(album))
        if resp.status_code not in (201, 405):  # 405: already exists
            resp.raise_for_status()

    def file_exists(self, nc_path):
        url = f"{self.files_url}/{quote(nc_path.strip('/'))}"
        resp = self._request("PROPFIND", url, headers={"Depth": "0"})
        if resp.status_code == 404:
            return False
        resp.raise_for_status()
        return True

    def add_to_album(self, album, nc_path):
        """Link an existing file. True if added, False if already present, None if
        the file is not on Nextcloud (yet)."""
        src = f"{self.files_url}/{quote(nc_path.strip('/'))}"
        dst = f"{self.album_url(album)}/{quote(os.path.basename(nc_path), safe='')}"
        resp = self._request("COPY", src, headers={"Destination": dst})
        if resp.status_code == 404:
            return None
        if resp.status_code in (409, 412):
            return False
        resp.raise_for_status()
        return True


def read_albums(csv_path):
    """Return {album title: [(fileName, sizeBytes or None, csv row), ...]}, duplicates removed."""
    albums = defaultdict(dict)
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            title = (row.get("sourceAlbumTitle") or "").strip().replace("/", "-")
            name = row.get("fileName") or ""
            if not title or not name:
                continue
            size = row.get("sizeBytes")
            albums[title][row.get("mediaKey") or name] = (
                name,
                int(size) if size and size.isdigit() else None,
                row,
            )
    return {t: list(files.values()) for t, files in albums.items()}


def index_local(root, skip_top=()):
    index = defaultdict(list)
    for dirpath, dirs, files in os.walk(root):
        if dirpath == root:
            dirs[:] = [d for d in dirs if d not in skip_top]
        for fn in files:
            index[fn].append(os.path.join(dirpath, fn))
    return index


def date_score(path, taken):
    """2 if a .../YYYY/MM/DD* folder matches taken, 1 for .../YYYY/MM, else 0."""
    parts = path.split(os.sep)[:-1]
    for i in range(len(parts) - 1):
        if parts[i] == f"{taken:%Y}" and parts[i + 1] == f"{taken:%m}":
            if any(p.startswith(f"{taken:%d}") for p in parts[i + 2 :]):
                return 2
            return 1
    return 0


def resolve(index, name, size, row=None, root=None, prefer=None):
    """Return (path, None) or (None, reason)."""
    cands = index.get(name, [])
    if len(cands) > 1 and size is not None:
        by_size = [c for c in cands if os.path.getsize(c) == size]
        if by_size:
            cands = by_size
    best = 0
    if len(cands) > 1 and row and (row.get("takenAt") or "").strip():
        taken = local_taken(row)
        scores = {c: date_score(c, taken) for c in cands}
        best = max(scores.values())
        if best:
            cands = [c for c in cands if scores[c] == best]
    if len(cands) > 1 and root and prefer:
        own = [
            c
            for c in cands
            if prefer in os.path.relpath(c, root).split(os.sep)[:-1]
        ]
        cands = own or cands
    if best == 2 and len(cands) > 1:
        deepest = max(c.count(os.sep) for c in cands)
        cands = [c for c in cands if c.count(os.sep) == deepest]
    if not cands:
        return None, "not found"
    return min(cands), None


UNSET_NAME = "[UNSET]"
UNSET_RE = re.compile(r"^\[UNSET\]\(\d+\)\.")


def index_unset(index):
    """{size: [paths]} of files named like Google's '[UNSET](N).ext' exports."""
    by_size = defaultdict(list)
    for fn, paths in index.items():
        if UNSET_RE.match(fn):
            for p in paths:
                try:
                    by_size[os.path.getsize(p)].append(p)
                except OSError:
                    pass
    return by_size


def take_unset(by_size, size, used):
    """First unused file with exactly this size, marked as used."""
    for p in sorted(by_size.get(size, [])):
        if p not in used:
            used.add(p)
            return p
    return None


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "album"


def local_taken(row):
    """takenAt (UTC) shifted by the csv timezone offset."""
    taken = (row.get("takenAt") or "").strip()
    if not taken:
        raise RuntimeError("no takenAt in csv, cannot choose a YYYY/MM/DD folder")
    utc = datetime.fromisoformat(taken.replace("Z", "+00:00"))
    off = int(row.get("timezoneOffsetMs") or 0)
    return utc.astimezone(timezone(timedelta(milliseconds=off)))


def target_dir(local_root, row, album):
    """<root>/YYYY/MM/DD_* (existing folder of that day preferred) else DD_gp-<album>."""
    taken = local_taken(row)
    month_dir = os.path.join(local_root, f"{taken:%Y}", f"{taken:%m}")
    day = f"{taken:%d}_"
    if os.path.isdir(month_dir):
        existing = sorted(d for d in os.listdir(month_dir) if d.startswith(day))
        if existing:
            return os.path.join(month_dir, existing[0])
    return os.path.join(month_dir, f"{day}gp-{slug(album)}")


def fetch_missing(name, size, row, title, fallback, local_root, dry_run, src=None):
    """Move a file missing from the local tree from the fallback folders into its
    YYYY/MM/DD_* folder. Returns the new path or None if no fallback has it.
    """
    src = src or resolve(fallback, name, size, row)[0]
    if not src:
        return None
    base = os.path.basename(src)
    dest = os.path.join(target_dir(local_root, row, title), base)
    if not dry_run:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.move(src, dest)
        fallback[base].remove(src)
    print(f"  {'would ' if dry_run else ''}move {base} -> {dest}")
    return dest


def print_summary(stats, created, dry_run):
    cols = [
        ("linked", "linked"),
        ("already", "already in album"),
        ("move", "moved"),
        ("waiting", "waiting for sync"),
        ("missing", "not found"),
        ("failed", "move failed"),
    ]
    total = Counter()
    for st in stats.values():
        total.update(st)
    print(f"\n== Summary{' (dry run)' if dry_run else ''} ==")
    print(f"albums: {len(stats)} ({created} created)")
    print(f"photos in csv: {total['total']}")
    for key, label in cols:
        print(f"  {label + ':':<20}{total[key]:>7}")
    width = max((len(t) for t in stats), default=5)
    print(f"\n{'album':<{width}}  total  linked  already    moved  missing")
    for title in sorted(stats):
        st = stats[title]
        missing = st["missing"] + st["failed"]
        print(
            f"{title:<{width}}  {st['total']:>5}  {st['linked']:>6}  "
            f"{st['already']:>7}  {st['move']:>7}  {missing:>7}"
        )


def main():
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("csv", help="GPTK album export csv")
    p.add_argument(
        "--local-root", required=True, help="local directory mirrored to Nextcloud"
    )
    p.add_argument(
        "--nc-root",
        default="/",
        help="Nextcloud path that --local-root corresponds to (default: /)",
    )
    p.add_argument(
        "--url", default=os.getenv("NC_URL"), help="Nextcloud base url (or NC_URL)"
    )
    p.add_argument(
        "--user", default=os.getenv("NC_USER"), help="Nextcloud user (or NC_USER)"
    )
    p.add_argument(
        "--skip-dir",
        action="append",
        default=[],
        help="top-level folder of --local-root that is not synced to Nextcloud and "
        "is ignored (repeatable)",
    )
    p.add_argument(
        "--prefer-folder",
        help="folder name preferred when a file name matches several local paths",
    )
    p.add_argument(
        "--fallback-dir",
        action="append",
        default=[],
        help="extra folder searched for files missing in --local-root; hits are moved "
        "into <local-root>/YYYY/MM/DD_* (repeatable)",
    )
    p.add_argument(
        "--wait",
        type=int,
        default=300,
        help="seconds to wait at the end for moved files to be synced by the "
        "Nextcloud client before linking them (default: 300)",
    )
    p.add_argument(
        "--dry-run", action="store_true", help="only report what would be done"
    )
    p.add_argument(
        "-v", "--verbose", action="store_true", help="log every HTTP request"
    )
    args = p.parse_args()

    if not args.url or not args.user:
        p.error("--url and --user (or NC_URL / NC_USER) are required")
    password = os.getenv("NC_PASS") or getpass("Nextcloud app password: ")

    albums = read_albums(args.csv)
    print(f"{len(albums)} albums in csv")

    print("Indexing local files ...")
    index = index_local(args.local_root, args.skip_dir)
    local_root = os.path.abspath(args.local_root)
    fallback = defaultdict(list)
    for d in args.fallback_dir:
        for fn, paths in index_local(d).items():
            fallback[fn].extend(paths)
    unset_local = index_unset(index)
    unset_fb = index_unset(fallback)
    used_unset = set()
    unsynced = set()  # files placed locally this run
    pending = []  # (album, name, nc_path) waiting for the client sync

    nc = Nextcloud(args.url, args.user, password, args.verbose)
    existing = nc.list_albums()
    stats = {t: Counter(total=len(f)) for t, f in albums.items()}
    created = 0

    for title in sorted(albums):
        st = stats[title]
        if title not in existing:
            print(f"create album: {title}")
            created += 1
            if not args.dry_run:
                nc.create_album(title)
        present = nc.list_album_files(title) if title in existing else set()

        for name, size, row in albums[title]:
            fb_src = None
            if name == UNSET_NAME:
                local = take_unset(unset_local, size, used_unset)
                reason = None if local else "not found"
                if not local:
                    fb_src = take_unset(unset_fb, size, used_unset)
            else:
                local, reason = resolve(
                    index, name, size, row, local_root, args.prefer_folder
                )
            if reason == "not found":
                try:
                    dest = fetch_missing(
                        name, size, row, title, fallback, local_root, args.dry_run,
                        fb_src,
                    )
                except (RuntimeError, OSError) as e:
                    print(f"  skip {name}: move failed ({e})", file=sys.stderr)
                    st["failed"] += 1
                    continue
                if dest:
                    st["move"] += 1
                    if args.dry_run:
                        continue
                    index[os.path.basename(dest)].append(dest)
                    unsynced.add(dest)
                    local, reason = dest, None
            if reason:
                print(f"  skip {name}: {reason}", file=sys.stderr)
                st["missing"] += 1
                continue
            name = os.path.basename(local)
            if any(e == name or e.endswith("-" + name) for e in present):
                st["already"] += 1
                continue
            rel = os.path.relpath(os.path.abspath(local), local_root).replace(
                os.sep, "/"
            )
            nc_path = f"{args.nc_root.rstrip('/')}/{rel}"
            if local in unsynced:
                pending.append((title, name, nc_path))
            elif args.dry_run:
                print(f"  would add {nc_path}")
                st["linked"] += 1
            else:
                added = nc.add_to_album(title, nc_path)
                if added is None:
                    pending.append((title, name, nc_path))
                elif added:
                    st["linked"] += 1
                else:
                    st["already"] += 1

    deadline = time.monotonic() + args.wait
    while pending:
        print(f"{len(pending)} moved files waiting for the Nextcloud sync ...")
        still = []
        for title, name, nc_path in pending:
            if nc.file_exists(nc_path):
                if nc.add_to_album(title, nc_path):
                    stats[title]["linked"] += 1
                print(f"  added {nc_path} to {title}")
            else:
                still.append((title, name, nc_path))
        pending = still
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(10)
    for title, _name, nc_path in pending:
        stats[title]["waiting"] += 1
        print(f"  not yet synced, rerun later: {nc_path} ({title})", file=sys.stderr)

    print_summary(stats, created, args.dry_run)


if __name__ == "__main__":
    main()
