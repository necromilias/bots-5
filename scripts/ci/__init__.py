"""CI v1 support machinery for the authoritative T4 sharded run.

Everything here is deliberately small, deterministic and standard-library only:
the correctness of "the shards really did run the whole test population" must be
inspectable and unit-testable without GitHub.
"""
