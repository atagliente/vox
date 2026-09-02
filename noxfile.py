"""Reproduce what CI does, locally, on whichever interpreters are installed.

CI is the authority, but waiting for a runner to tell you the formatting is
off is a slow way to find out. `nox` runs the same four checks here:

    nox              lint, types, tests on every Python found
    nox -s tests     just the tests, on every Python found
    nox -s tests-3.12
    nox -s coverage  the run that has to clear the floor in pyproject.toml

Interpreters that are not installed are skipped, not failed: nobody has all
three lying around, and CI covers the ones you do not.
"""

from __future__ import annotations

import nox

nox.options.sessions = ["lint", "types", "tests"]
nox.options.reuse_existing_virtualenvs = True

PYTHONS = ["3.11", "3.12", "3.13"]


@nox.session
def lint(session: nox.Session) -> None:
    session.install("ruff")
    session.run("ruff", "check", ".")
    session.run("ruff", "format", "--check", ".")


@nox.session
def types(session: nox.Session) -> None:
    session.install("-e", ".[dev]")
    session.run("mypy")


@nox.session(python=PYTHONS)
def tests(session: nox.Session) -> None:
    session.install("-e", ".[dev]")
    session.run("pytest", "-q", "--no-cov", *session.posargs)


@nox.session
def coverage(session: nox.Session) -> None:
    session.install("-e", ".[dev]")
    session.run("pytest", "-q", "--cov", "--cov-report=term-missing", *session.posargs)
