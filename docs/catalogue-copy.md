# Catalogue copy: design decisions and defects

Moved here from the README, unchanged. The code is `app/ai/catalog_copy.py`; the tests are `tests/test_catalog_copy.py` and `tests/test_catalog_copy_transport.py`.

## Design decisions

Around that check: the prompt receives the facts and never the whole product record,
so a private cost field cannot leak into public copy; length is enforced in code
after generation instead of trusted from the prompt; the transport is an
OpenAI-compatible chat completion, so the provider is a base URL and a model name;
and the default model is set in the module (Groq, `openai/gpt-oss-120b`, because
`llama-3.3-70b-versatile` disappeared from the account's model list).

## Defects the tests found

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
