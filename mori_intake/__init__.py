"""mori-intake — agent-memory ingest front door.

A physically-separate FastAPI service that accepts agent-originated memory
proposals and buffers them in its own Postgres store.  Only a deliberate
promotion path moves anything into mori canon; agent writes never touch
the ``memories`` table directly.
"""
