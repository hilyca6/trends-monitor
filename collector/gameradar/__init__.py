"""Game-name radar: discover emerging web-game names from game portals.

Ports the discovery half of the `game-name-radar` tool into this project's
Python pipeline. Verification is NOT reimplemented here — discovered names are
exported to ``web/data/gameradar.json`` and then enriched by the existing
``advice`` (Bing SERP competition / market gap / name risk + ★stars) and
``fastheat`` layers, exactly like the other lists.
"""
