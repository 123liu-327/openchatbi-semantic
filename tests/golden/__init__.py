"""Golden (contract) tests.

Everything in this package freezes behaviour that the fork's incremental work
depends on. A failure here means a refactor changed something users can observe;
either revert it or update the frozen contract on purpose.
"""
