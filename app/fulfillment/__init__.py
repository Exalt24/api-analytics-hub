"""Orders sent to a fulfilment partner, and the partner's status events. An order is an opaque reference and a clinic: no personal or health detail is stored, audited or logged.

The send and its dead letter are service.submit_order over client.PartnerClient; the state machine is states.py (and the rows of fulfillment_transition); the receiver for the partner's
signed events is webhook.py; the hash chained audit log is audit.py. The contract with the partner and the limits: docs/fulfillment.md.
"""
