"""Prints THIRD_PARTY_NOTICES.txt for a Securities release.

Runs in the build image (packaging/release.sh), where Python, the pinned libraries of
requirements.lock and Nuitka are all installed:

    python3 notices.py requirements.lock
"""
import importlib.metadata as md
import re
import sys
from pathlib import Path

RULE = "\n" + "=" * 79 + "\n\n"
LICENSE_NAME = re.compile(r"LICEN[CS]E|COPYING|NOTICE", re.IGNORECASE)


def pinned(lock):
    for line in Path(lock).read_text().splitlines():
        if m := re.match(r"^([A-Za-z0-9_.-]+)==", line):
            yield m.group(1)


def license_of(meta):
    if expr := meta.get("License-Expression"):
        return expr
    text = (meta.get("License") or "").strip()
    if text and "\n" not in text and len(text) < 80:
        return text
    classifiers = [c.split(" :: ")[-1] for c in meta.get_all("Classifier") or []
                   if c.startswith("License ::")]
    return ", ".join(classifiers) or "see the license text below"


def license_files(dist):
    files = [f for f in dist.files or [] if ".dist-info" in str(f) and LICENSE_NAME.search(f.name)]
    for f in sorted(files, key=str):
        text = Path(dist.locate_file(f)).read_text(errors="replace").strip()
        yield f.name, text


def main(lock):
    out = sys.stdout
    out.write("Third-party software in the Securities release\n\n")
    out.write(
        "premarket.bin and strategies.bin are this project's code compiled with Nuitka. They\n"
        "link in the Python 3.12 interpreter and Nuitka's runtime library, and import the\n"
        "Python standard library and the libraries below at run time. The securities image\n"
        "carries all of them; the tarball carries only the two programs, and pip installs the\n"
        "libraries from requirements.lock.\n"
    )

    out.write(RULE)
    out.write(f"Python {sys.version.split()[0]} (https://www.python.org), as packaged by Ubuntu:\n"
              "linked into premarket.bin and strategies.bin, and the interpreter of the image\n\n")
    out.write(Path("/usr/share/doc/libpython3.12-stdlib/copyright").read_text().strip() + "\n")

    nuitka = md.distribution("nuitka")
    out.write(RULE)
    out.write(f"Nuitka {nuitka.version} (https://nuitka.net): its runtime library is compiled into\n"
              "premarket.bin and strategies.bin. The runtime library is licensed under AGPLv3 with\n"
              "the Nuitka Runtime Library Exception below, which permits conveying the compiled\n"
              "programs under their own license. The Nuitka compiler itself is not distributed.\n")
    for name, text in license_files(nuitka):
        if name in ("NOTICE.txt", "LICENSE-RUNTIME.txt"):
            out.write(f"\n--- {name}\n\n{text}\n")

    for name in sorted(pinned(lock), key=str.lower):
        dist = md.distribution(name)
        meta = dist.metadata
        home = meta.get("Home-page") or next(
            (u.split(", ", 1)[1] for u in meta.get_all("Project-URL") or []
             if u.lower().startswith(("homepage", "source", "repository"))), "")
        out.write(RULE)
        out.write(f"{meta['Name']} {dist.version}{f' ({home})' if home else ''}\n")
        out.write(f"License: {license_of(meta)}\n")
        for fname, text in license_files(dist):
            out.write(f"\n--- {fname}\n\n{text}\n")

    out.write(RULE)
    out.write(
        "The interpreter also links dynamically against zlib and expat. The image carries\n"
        "their licenses, and those of every Ubuntu package in it, in /usr/share/doc.\n"
        "psycopg-binary, numpy and pyarrow bundle native libraries (libpq and its\n"
        "dependencies, OpenBLAS, Arrow C++); the notices those wheels ship are included above.\n"
    )


if __name__ == "__main__":
    main(sys.argv[1])
