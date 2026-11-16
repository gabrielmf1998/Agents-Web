#!/usr/bin/env python3
"""Write DIR/release.json, a release in the format of the GitHub API, for the files in DIR (and SHA256SUMS if it
is missing). The install and update tests point AGENTS_WEB_RELEASES_API at it.
Usage: fake-release.py DIR [URL]   URL is where DIR is served (default: its file:// URI)"""
import hashlib, json, re, sys
from pathlib import Path

d = Path(sys.argv[1]).resolve()
base = sys.argv[2].rstrip("/") if len(sys.argv) > 2 else d.as_uri()
pat = re.compile(r"^(agents-web[-_]|Agents_Web-|AgentsWeb-Setup-)(\d+\.\d+\.\d+)[-_.]")
files = sorted(p for p in d.iterdir() if p.is_file() and pat.match(p.name))
if not files:
    sys.exit(f"no release files in {d}")
version = pat.match(files[0].name).group(2)
sums = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in files}
if not (d / "SHA256SUMS").exists():
    (d / "SHA256SUMS").write_text("".join(f"{h}  {n}\n" for n, h in sums.items()))
assets = [{"name": n, "browser_download_url": f"{base}/{n}", "digest": f"sha256:{h}"} for n, h in sums.items()]
assets.append({"name": "SHA256SUMS", "browser_download_url": f"{base}/SHA256SUMS", "digest": None})
(d / "release.json").write_text(json.dumps({"tag_name": f"v{version}", "name": f"Agents Web {version}",
                                            "body": f"Test release {version}", "assets": assets}, indent=1))
print(f"{d / 'release.json'}: v{version}, {len(files)} files")
