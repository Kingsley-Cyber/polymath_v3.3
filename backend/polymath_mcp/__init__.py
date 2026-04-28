"""Polymath MCP sidecar — Phase 8.

Exposes the same retrieval and graph-extraction service layer that the FastAPI
backend uses, over the Model Context Protocol. Sidecar process; runs in its own
docker-compose service. No business logic — every tool delegates to backend.services.
"""
