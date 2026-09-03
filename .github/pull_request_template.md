## What this changes

<!-- The effect on someone using VOX, not the diff. -->

## Why

<!-- What was wrong, or what was missing. If the obvious approach was not
     taken, say what it was and why it lost — that reasoning is worth more in
     review than anywhere else. -->

## Checks

- [ ] `ruff check . && ruff format --check .`
- [ ] `mypy`
- [ ] `pytest -q`
- [ ] There is a test that fails without this change
- [ ] Anything this deliberately does *not* do is written down, in the code
      or in `cr.md`

## Anything to be careful about

<!-- A limit, a platform it was not tried on, a decision somebody else should
     look at. "Nothing" is a fine answer. -->
