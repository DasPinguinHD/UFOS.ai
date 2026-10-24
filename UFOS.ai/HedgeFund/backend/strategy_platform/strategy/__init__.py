"""Strategy definition (stage 2, SPEC §7): spec schema, DSL, validator,
numpy evaluator, portfolio construction, SDK, universes, alpha library.

Modules import nothing heavy at import time (numpy is imported lazily inside
functions) because main.py imports the API that imports this package.

Part of UFOS.ai, a proof-of-concept portfolio project; not investment advice.
"""
