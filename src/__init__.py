"""src — FastAPI gateway wrapping hermes AIAgent.

Exposes POST /api/v1/session and POST /api/v1/chat.
Streams SSE to the workflow-bff gateway (and from there to
digital-factory-ui).
"""
