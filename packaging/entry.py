"""The entry point PyInstaller bundles.

`pyinstaller -m vox_chat` is not a thing, and pointing it at
`vox_chat/__main__.py` makes the package a script and breaks every relative
import in it. A three-line file that imports the package properly is the
smallest thing that works, and it lives here rather than in `vox_chat/`
because it is a packaging detail, not part of VOX.
"""

from vox_chat.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
