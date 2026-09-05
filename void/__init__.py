"""V.O.I.D - a local-first personal AI assistant (V1).

The package is organized into replaceable layers:

    void.config       - configuration loading
    void.security     - secret storage, risk gating
    void.providers    - swappable LLM backends (Gemini, local)
    void.actions      - things V.O.I.D can do on the machine (files, apps)
    void.core         - the agent loop, task engine, kill switch
    void.ui           - minimal desktop widget

V1 goal: prove the concept end-to-end - natural-language goal in,
autonomous Windows action out, with an always-available emergency stop.
"""

__version__ = "0.1.0"
