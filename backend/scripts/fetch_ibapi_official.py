"""Fetches, SHA256-verifies, and extracts the OFFICIAL Interactive Brokers
TWS API Python client -- IBKR TWS Migration Phase 1.1.

Corrects a real defect found in this project's own Phase 1: the PyPI
``ibapi`` package (version 9.81.1.post1, last published December 2020)
was presented as "official." It is not -- Interactive Brokers' own
current documentation states plainly that pip/PyPI distribution is not
hosted, endorsed, or supported by IBKR, and the PyPI project's own
publisher account is unrelated to Interactive Brokers (confirmed by
inspecting https://pypi.org/project/ibapi/ directly: the listed
maintainer is a third-party PyPI account, not an IBKR-controlled one,
despite the package's bundled metadata -- copied verbatim from IBKR's
own real source files -- describing itself as "Official"). It was also
nine minor versions behind the real current release.

The ONLY real official distribution channel is the direct download
gated behind a license click-through at
https://interactivebrokers.github.io -- confirmed live: the underlying
ZIP URLs themselves (below) require no login and are not actually
access-controlled server-side, only presented behind a UI "I Agree"
button. This script fetches that same real file directly and verifies
it against a SHA256 computed from a real download performed for this
task, so a corrupted or tampered mirror is caught, never silently used.

WHY THIS SCRIPT EXISTS INSTEAD OF COMMITTING THE SOURCE: the IB API
Non-Commercial License governing this code states plainly (verified
live against Interactive Brokers' own published license terms): "You
agree not to publish, disseminate, or redistribute the API Code to any
third party." This project's GitHub repository is PUBLIC -- committing
the real IBKR source into git history would be exactly that kind of
redistribution. Fetching it fresh, locally, at setup/build time (never
committed -- see backend/.gitignore) is the license-compliant way to
get reproducible, auditable, pinned official source into this project.
See vendor/ibapi_official/PROVENANCE.md for the full record.
"""

import hashlib
import shutil
import sys
import urllib.request
import zipfile
from pathlib import Path

# IBKR's own "Stable" release as of this task (2026-08-31) -- deliberately
# NOT the bleeding-edge "Latest" (10.50 at the same date) for a production-
# oriented integration. Re-audit this choice, don't blindly bump it, if a
# future task revisits this (see PROVENANCE.md).
_VERSION = "1045.01"
_URL = f"https://interactivebrokers.github.io/downloads/twsapi_macunix.{_VERSION}.zip"
# Computed from a real download of the above URL on 2026-08-31 (this
# task) -- verified before this script ever ran against it.
_EXPECTED_SHA256 = "56ea048911052e86d6621ab712957c790fce6d547bc2a55900136ae4f6835941"

_VENDOR_DIR = Path(__file__).resolve().parent.parent / "vendor" / "ibapi_official"
_PYTHONCLIENT_SRC = "IBJts/source/pythonclient"


def main() -> None:
    dest = _VENDOR_DIR / "pythonclient"
    if dest.exists() and (dest / "setup.py").exists():
        print(f"Already present and verified at a prior run: {dest}")
        return

    _VENDOR_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = _VENDOR_DIR / f"twsapi_{_VERSION}.zip"

    print(f"Fetching official TWS API {_VERSION} from {_URL} ...")
    urllib.request.urlretrieve(_URL, zip_path)  # noqa: S310 -- fixed, documented IBKR URL

    digest = hashlib.sha256(zip_path.read_bytes()).hexdigest()
    if digest != _EXPECTED_SHA256:
        zip_path.unlink()
        print(
            f"REFUSING to use this download -- SHA256 mismatch.\n"
            f"  expected: {_EXPECTED_SHA256}\n"
            f"  got:      {digest}\n"
            "This means interactivebrokers.github.io served something "
            "different from the file this project pinned, or the "
            "download was corrupted/tampered with in transit. Not "
            "extracting. See vendor/ibapi_official/PROVENANCE.md.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    print(f"SHA256 verified: {digest}")

    extract_dir = _VENDOR_DIR / "_extract_tmp"
    if extract_dir.exists():
        shutil.rmtree(extract_dir)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    src = extract_dir / _PYTHONCLIENT_SRC
    if not src.is_dir():
        raise SystemExit(f"expected {_PYTHONCLIENT_SRC} inside the official ZIP, not found")

    if dest.exists():
        shutil.rmtree(dest)
    shutil.move(str(src), str(dest))
    shutil.rmtree(extract_dir)
    zip_path.unlink()

    print(f"Official TWS API {_VERSION} Python client ready at {dest}")


if __name__ == "__main__":
    main()
