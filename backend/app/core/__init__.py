"""Cross-cutting concerns: configuration, logging, errors, security, sanitising.

Nothing in here imports a domain module. The dependency runs one way, so the
crawler, the retriever, and the generator can all depend on these guarantees
without depending on each other.
"""
