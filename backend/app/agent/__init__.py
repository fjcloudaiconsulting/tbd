"""Agent tool registry and tools (TBD-559).

Importing the package registers every tool, so no consumer can see a
partially populated registry.
"""
from app.agent import tools  # noqa: F401
