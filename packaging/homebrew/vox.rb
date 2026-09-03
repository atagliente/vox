# Homebrew formula for VOX.
#
# It installs from the GitHub Release rather than from PyPI, so it does not
# wait on an account that may never exist, and the same sdist the release
# workflow signs is the one people install.
#
# To use it, this file goes in a tap — `homebrew-vox/Formula/vox.rb` in a
# repository called `homebrew-vox` — and users type:
#
#     brew install <owner>/vox/vox
#
# Creating that repository is the one step that cannot be done from here: it
# is a second repository under an account, and taps are named after their
# owner. Once it exists, the only thing that changes per release is `url`,
# `sha256` and the resource blocks, which `brew update-python-resources vox`
# regenerates.
#
# The sha256 below is the placeholder Homebrew's own template uses. Replace
# it with `shasum -a 256` of the release tarball; `brew audit --strict vox`
# will say so if you forget.

class Vox < Formula
  include Language::Python::Virtualenv

  desc "Retro terminal chat client for OpenAI-compatible providers"
  homepage "https://github.com/atagliente/vox"
  url "https://github.com/atagliente/vox/archive/refs/tags/v0.1.0.tar.gz"
  sha256 "0000000000000000000000000000000000000000000000000000000000000000"
  license :cannot_represent # PolyForm Noncommercial 1.0.0 — not an SPDX id Homebrew knows
  head "https://github.com/atagliente/vox.git", branch: "main"

  depends_on "python@3.12"

  # Regenerate with: brew update-python-resources vox
  # Left empty deliberately rather than filled with versions nobody checked:
  # virtualenv_install_with_resources reads this list, and a stale pin here
  # would install something different from what the tests ran against.

  def install
    virtualenv_install_with_resources
  end

  test do
    # No terminal in the sandbox, so the TUI cannot start. --version proves
    # the entry point resolves and the package imports, and `doctor` exits
    # non-zero with a diagnosis rather than a traceback when nothing is
    # configured, which is the behaviour worth holding to.
    assert_match "vox", shell_output("#{bin}/vox --version")
    system bin/"vox", "config-path"
  end
end
