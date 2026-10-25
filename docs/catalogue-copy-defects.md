# Defects the catalogue copy tests found

Moved here from the README, unchanged. The code is `app/ai/catalog_copy.py`; the tests are `tests/test_catalog_copy.py` and `tests/test_catalog_copy_transport.py`.

The tests found three defects in the first version:

- **Digits inside chemical formulas read as quantities.** The 2 and 3 in `Al2O3` were
  flagged as invented numbers, which fires the guard on every formula and gets it
  switched off. The number pattern now ignores a digit that follows a letter, and a
  test keeps `38ohms` written without a space still checked.
- **A reasoning model spent 158 of its 160 completion tokens on its reasoning trace**
  and returned empty content with a normal-looking response. The token budget is now
  larger, the effort is set low, and a truncated empty completion is reported as a
  truncation instead of a bad description.
- **Numbers written as words** (`five`, `hundred`) walked straight past a digit-only
  check, so number words are verified against the facts too.
