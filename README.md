# gptk2nextcloud

Mirror Google Photos albums to Nextcloud Photos albums.

The album list comes from a [Google Photos Toolkit (GPTK)](https://github.com/xob0t/Google-Photos-Toolkit) CSV export. No file is uploaded: the script creates the albums on Nextcloud and links photos that already exist in your Nextcloud files (via the Photos DAV API), so a local folder synced with Nextcloud is required.

## Install

Requires Python 3 and a Nextcloud server with the Photos app.

```bash
git clone https://github.com/an-dr-eas-k/gptk2nextcloud.git
cd gptk2nextcloud
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Create an app password in Nextcloud under *Settings > Security > Devices & sessions*.

## Usage

Set the credentials (the password is prompted for if `NC_PASS` is unset):

```bash
export NC_URL=https://cloud.example.com
export NC_USER=alice
export NC_PASS=xxxxx-xxxxx-xxxxx-xxxxx-xxxxx
```

Preview first, then run for real:

```bash
python gptk2nextcloud.py albums.csv --local-root ~/Nextcloud/Photos --nc-root /Photos --dry-run
python gptk2nextcloud.py albums.csv --local-root ~/Nextcloud/Photos --nc-root /Photos
```

### Options

| Option | Description |
| --- | --- |
| `csv` | GPTK album export CSV (needs the columns `sourceAlbumTitle`, `fileName`, `mediaKey`, `sizeBytes`, `takenAt`, `timezoneOffsetMs`) |
| `--local-root` | Local directory that is synced to Nextcloud (required) |
| `--nc-root` | Nextcloud path that `--local-root` corresponds to (default `/`) |
| `--url`, `--user` | Nextcloud base URL and user (or `NC_URL`, `NC_USER`) |
| `--skip-dir` | Top-level folder of `--local-root` that is not synced to Nextcloud and is ignored (repeatable) |
| `--prefer-folder` | Folder name preferred when a file name matches several local paths |
| `--fallback-dir` | Extra folder searched for files missing in `--local-root`; hits are moved into `<local-root>/YYYY/MM/DD_*` (repeatable) |
| `--wait` | Seconds to wait at the end for moved files to be synced by the Nextcloud client before linking them (default 300) |
| `--dry-run` | Only report what would be done |
| `-v`, `--verbose` | Log every HTTP request |

### How files are matched

- Files are looked up by name in `--local-root`. Ties are resolved by file size, then by a matching `YYYY/MM/DD` folder, then by `--prefer-folder`.
- Files not found are searched in the `--fallback-dir` folders and moved into the dated folder tree. They are linked once the Nextcloud client has synced them; if that does not finish within `--wait`, rerun the script later.
- The script is safe to rerun: existing albums and files already in an album are skipped.
- A summary per album is printed at the end.
