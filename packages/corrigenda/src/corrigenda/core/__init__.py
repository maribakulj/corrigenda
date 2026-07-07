"""Pure core (§3): zero I/O, zero network, zero lxml.

Schemas, guards, hyphenation reconciliation, chunk planning, response
validation, orchestration, and the ports consumers implement. The
import-contract test guarantees that importing anything under
``corrigenda.core`` never loads lxml or a format module.
"""
