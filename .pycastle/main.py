"""PyCastle workflow for this repository.

Hand-written for the walking skeleton: a single ``implement`` phase. Edit this
file with normal Python to change the workflow — add phases, reorder them, or
(in a later slice) wire up success and failure transitions.
"""

from pycastle import graph as g

graph = g.build().phase("implement", prompt="implement.md").build()
