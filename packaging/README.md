# packaging

Everything here is about getting VOX onto somebody else's machine. None of it
is imported by VOX.

| | |
| --- | --- |
| `entry.py` | the three lines PyInstaller bundles, so `--onefile` has a script to point at that does not turn the package into one |
| `homebrew/vox.rb` | a Homebrew formula, ready for a tap |
| `aur/PKGBUILD` | an AUR package, ready for an AUR account |

## What is left, and why

Both of the last two are complete files that cannot be *published* from here,
and the reason is the same in both cases: publishing means creating an account
or a repository that belongs to a person.

- **Homebrew** needs a tap — a second repository named `homebrew-vox` under
  the same account. The formula is written against the GitHub Release tarball,
  so it does not wait on PyPI. What changes per release is `url` and `sha256`.
- **AUR** needs an AUR account with an SSH key registered to it. The `PKGBUILD`
  carries the commands for the first push in its header.

Neither file carries a made-up checksum. Homebrew's is the zero placeholder its
own template uses and `brew audit --strict` complains about; the AUR's is
`SKIP`, which `updpkgsums` replaces. A plausible-looking wrong number would
install the wrong thing quietly, which is the failure worth avoiding.

## The executables

`.github/workflows/release.yml` builds a single-file binary for Linux, macOS
and Windows on a tag, signs it with sigstore, and attaches it to the Release.
They are 40-60 MB each because they contain a Python and a `cryptography`
wheel, which is the price of not needing either installed.
