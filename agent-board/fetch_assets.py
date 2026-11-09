#!/usr/bin/env python3
"""Downloads the Windows 98 look: 98.css (MIT, from npm) and original Windows 98 icons.

They are not stored in the repository; run once after cloning (install.ps1 does it).
"""
import hashlib
import io
import sys
import tarfile
import urllib.request
from pathlib import Path

WEB = Path(__file__).resolve().parent / "web"
CSS_URL = "https://registry.npmjs.org/98.css/-/98.css-0.1.21.tgz"
CSS_SHA256 = "bffc701cd55c38688c9eba5bf4cd34f26158f5895904166356cc156742082d74"
CSS_FILES = ["98.css", "ms_sans_serif.woff", "ms_sans_serif.woff2", "ms_sans_serif_bold.woff", "ms_sans_serif_bold.woff2"]
ICON_URL = "https://win98icons.alexmeub.com/icons/png/{}.png"
ICONS = """
application_hourglass-1 check-0 check-1 computer-1 directory_closed-1 envelope_closed-1
help_book_small-0 help_book_small-1 loudspeaker_muted-0 loudspeaker_rays-0 msg_error-0 msg_error-2
msg_information-0 msg_question-0 msg_question-2 msg_warning-2 network_normal_two_pcs-0
network_normal_two_pcs-1 notepad-1 recycle_bin_full-0 users-0 world-0 world-1
""".split()


def get(url):
    # The icon site rejects requests without a browser User-Agent.
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def main():
    vendor = WEB / "vendor"
    icons = WEB / "icons"
    vendor.mkdir(parents=True, exist_ok=True)
    icons.mkdir(parents=True, exist_ok=True)

    if not all((vendor / f).exists() for f in CSS_FILES):
        blob = get(CSS_URL)
        if hashlib.sha256(blob).hexdigest() != CSS_SHA256:
            sys.exit("98.css checksum mismatch")
        with tarfile.open(fileobj=io.BytesIO(blob)) as tar:
            for name in CSS_FILES:
                (vendor / name).write_bytes(tar.extractfile(f"package/dist/{name}").read())
        print("98.css: ok")

    missing = [n for n in ICONS if not (icons / f"{n}.png").exists()]
    for n in missing:
        png = get(ICON_URL.format(n))
        if not png.startswith(b"\x89PNG"):
            sys.exit(f"icon {n}: not a PNG")
        (icons / f"{n}.png").write_bytes(png)
    print(f"icons: {len(ICONS)} ok ({len(missing)} downloaded)")


if __name__ == "__main__":
    main()
