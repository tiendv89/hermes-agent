"""Profiles — separate agent profiles with distinct tool registrations.

Each profile has its own ``setup.py`` that exposes two functions:

- ``register_tools(ctx)`` — registers the profile's tool set via the shared
  ``plugins.register()`` entry point.
- ``build_router()`` — returns a FastAPI ``APIRouter`` with the profile's
  endpoints.

The active profile is selected at startup via the ``HERMES_PROFILE``
environment variable (default: ``workflow``).
"""
