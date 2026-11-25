"""Coordination primitives for several AI coding agents sharing one machine.

Rename the whole tool by changing NAME (and the two lines marked in pyproject.toml):
the CLI prog name, environment-variable prefix, state directory and project marker all derive from it.
"""
NAME = 'agentlanes'
__version__ = '0.3.2'

ENV = NAME.upper()            # AGENTLANES_STATE_DIR, AGENTLANES_AGENT, ...
PROJECT_MARKER = '.' + NAME   # a directory named .agentlanes makes a per-project state dir
