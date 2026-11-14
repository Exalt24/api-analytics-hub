"""E-signature slice: a signing request for a document, a signature record, and a hash chained audit log.

The tables are migrations/016_esign.sql. It runs against a local Postgres with no vendor account: it is a tamper evident signing record, not a certified
e-signature service and not legal advice (docs/esign.md says what it does not do).
"""
