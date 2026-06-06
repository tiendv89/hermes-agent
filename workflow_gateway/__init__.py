"""workflow_gateway — FastAPI gateway wrapping hermes AIAgent.

Exposes POST /api/v5/create_session and POST /api/v5/stream_chat.
Streams SSE in the voyager envelope to workflow-backend (and from there
to digital-factory-ui).
"""
