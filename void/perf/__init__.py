"""Privacy-safe performance telemetry (V2.0). See ``void.perf.schema`` for the
allowlist that guarantees no content is ever recorded."""
from void.perf.sink import (  # noqa: F401
    configure, current_interaction_id, emit, interaction, new_interaction_id,
    shutdown, stats,
)
