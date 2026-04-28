"""
Polymath — Modal cloud embedder
────────────────────────────────────────────────────────────────────────────
Hosts Qwen3-Embedding-0.6B on a Modal GPU and exposes an OpenAI-compatible
/embeddings endpoint, identical to the local `embedder/` sidecar.

Two ways to deploy:

  (a) CLI (legacy):
      $ pip install modal
      $ modal setup            # one-time — authenticates you with modal.com
      $ modal deploy modal_embedder.py

      The module-level `app` symbol below is what the CLI picks up. It is
      built from POLYMATH_* env vars at import time.

  (b) Programmatic (Phase 22):
      from modal_embedder import build_app
      app = build_app(app_name="polymath-embedder", gpu_tier="L40S", ...)
      modal.runner.deploy_app(app, name=app.name, client=client)

      `build_app` is a pure factory — call it N times to construct N distinct
      apps with different params. Used by `backend/services/modal_deployer.py`
      to back the /api/infrastructure/modal/deploy endpoint.

Contract (matches the local embedder exactly):
    POST /embeddings    {input: str|list[str], model?: str}
                        → {data: [{index, embedding}], model, usage}
    GET  /health        → {status, model, dimension}
    GET  /info          → {model_name, dimension, device, batch_size}

Invariants the backend enforces (see `backend/services/embedder.py`):
    - `response.data[i].embedding` length == corpus frozen dimension (1024)
    - `response.model` matches the expected_model_id if caller passed one
"""

from __future__ import annotations

import os
from typing import Union

import modal


def _preload_weights() -> None:
    """Image-build hook — downloads the sentence-transformers weights into
    the Modal image layer so containers cold-start without a HuggingFace
    fetch. Runs inside Modal's builder, not in-process. Must be a real
    module-level function (Modal 0.64 rejects lambdas here)."""
    import os
    from sentence_transformers import SentenceTransformer

    model_id = os.getenv("POLYMATH_MODEL_ID_BUILD") or os.getenv(
        "POLYMATH_MODEL_ID", "Qwen/Qwen3-Embedding-0.6B"
    )
    SentenceTransformer(model_id)


def build_app(
    *,
    app_name: str = "polymath-embedder",
    model_id: str = "Qwen/Qwen3-Embedding-0.6B",
    gpu_tier: str = "T4",
    max_containers: int = 10,
    min_containers: int = 0,
    idle_timeout: int = 300,
    concurrency: int = 4,
    use_auth: bool = False,
) -> modal.App:
    """Construct a fresh `modal.App` with the given deploy parameters.

    Re-entrant: call multiple times to build multiple independent apps. All
    decorators inside capture `app` via closure, so no module-scope state is
    carried between calls. This is what the programmatic deploy path relies
    on — the CLI path at the bottom of the file calls this once with values
    from POLYMATH_* env vars.

    Args:
        app_name: Modal App name; determines the deployed URL:
            https://<workspace>--<app_name>-serve.modal.run
        model_id: HuggingFace model id. Any sentence-transformers-compatible
            model works. Dimension is introspected at startup — must match
            the corpus's frozen embedding_dimension.
        gpu_tier: one of T4 | L4 | A10G | L40S | A100 | H100.
        max_containers: autoscale ceiling for the GPU tier.
        min_containers: warm fleet size. 0 = fully scale-to-zero. Biggest
            idle-cost lever.
        idle_timeout: seconds before Modal scales an idle container down.
        concurrency: in-flight requests per container (VRAM permitting).
        use_auth: when True, the deployed endpoint requires a Bearer token
            matching the `MODAL_PROXY_KEY` secret.
    """
    # ── Image ──────────────────────────────────────────────────────────────
    # Pre-download weights into the image so cold starts don't re-fetch.
    # Modal 0.64's Image.run_function rejects lambdas — the preload fn
    # must be a real module-level def. Closure isn't allowed here either
    # (the fn runs inside the image builder, not in-process), so we read
    # the model id from an env var Modal sets on the builder.
    import os as _os

    _os.environ["POLYMATH_MODEL_ID_BUILD"] = model_id
    image = (
        modal.Image.debian_slim(python_version="3.11")
        .pip_install(
            "fastapi==0.104.1",
            "pydantic==2.5.0",
            "sentence-transformers==2.7.0",
            "torch==2.2.1",
            "transformers==4.39.3",
        )
        .run_function(_preload_weights, secrets=[])
    )

    app = modal.App(app_name, image=image)

    # Optional shared API key — `modal secret create polymath-embedder
    # --env MODAL_PROXY_KEY=<secret>` if you want to auth-gate the endpoint.
    auth_secret = (
        modal.Secret.from_name("polymath-embedder", required_keys=["MODAL_PROXY_KEY"])
        if use_auth
        else None
    )

    @app.cls(
        gpu=gpu_tier,
        image=image,
        # Modal 0.64 kwarg names (older than the 0.65+ rename to
        # scaledown_window / min_containers / max_containers):
        container_idle_timeout=idle_timeout,
        timeout=600,
        keep_warm=min_containers,
        concurrency_limit=max_containers,
        allow_concurrent_inputs=concurrency,
        secrets=[auth_secret] if auth_secret is not None else [],
        # Factory-built classes live inside build_app's closure; serialize
        # the code so Modal can ship it to the container without
        # requiring it at module scope.
        serialized=True,
    )
    class Embedder:
        """GPU-resident embedder. Modal keeps one instance warm per container."""

        @modal.enter()
        def startup(self):
            import time
            from sentence_transformers import SentenceTransformer

            t0 = time.time()
            self.model = SentenceTransformer(model_id, device="cuda")
            self.dim = self.model.get_sentence_embedding_dimension()
            self.model_id = model_id
            print(f"[embedder] loaded {model_id} dim={self.dim} in {time.time() - t0:.1f}s")

        @modal.method()
        def encode(self, texts: list[str]) -> list[list[float]]:
            vectors = self.model.encode(
                texts,
                batch_size=32,
                normalize_embeddings=True,
                show_progress_bar=False,
            )
            return [v.tolist() for v in vectors]

        @modal.method()
        def meta(self) -> dict:
            return {"model": self.model_id, "dim": self.dim}

    # ── FastAPI app exposed as Modal web endpoint ─────────────────────────
    web_app_image = (
        modal.Image.debian_slim(python_version="3.11")
        .pip_install("fastapi==0.104.1", "pydantic==2.5.0")
    )

    @app.function(
        image=web_app_image,
        container_idle_timeout=idle_timeout,
        concurrency_limit=max(5, max_containers // 4),
        serialized=True,
    )
    @modal.asgi_app()
    def serve():
        """Public FastAPI serving /embeddings /health /info."""
        from fastapi import FastAPI, HTTPException, Header
        from pydantic import BaseModel

        web = FastAPI(title="Polymath Modal Embedder")
        embedder = Embedder()

        class EmbReq(BaseModel):
            input: Union[str, list[str]]
            model: str | None = None

        class EmbObj(BaseModel):
            object: str = "embedding"
            index: int
            embedding: list[float]

        class EmbUsage(BaseModel):
            prompt_tokens: int
            total_tokens: int

        class EmbResp(BaseModel):
            object: str = "list"
            data: list[EmbObj]
            model: str
            usage: EmbUsage

        def _check_auth(authorization: str | None):
            secret = os.getenv("MODAL_PROXY_KEY")
            if not secret:
                return
            if not authorization or not authorization.startswith("Bearer "):
                raise HTTPException(status_code=401, detail="Missing bearer token")
            if authorization.split(" ", 1)[1].strip() != secret:
                raise HTTPException(status_code=401, detail="Invalid token")

        @web.get("/health")
        def health(authorization: str | None = Header(default=None)):
            _check_auth(authorization)
            meta = embedder.meta.remote()
            return {"status": "ok", **meta}

        @web.get("/info")
        def info(authorization: str | None = Header(default=None)):
            _check_auth(authorization)
            meta = embedder.meta.remote()
            return {
                "model_name": meta["model"],
                "dimension": meta["dim"],
                "device": "cuda",
                "batch_size": 32,
            }

        @web.post("/embeddings", response_model=EmbResp)
        def embed(req: EmbReq, authorization: str | None = Header(default=None)):
            _check_auth(authorization)
            texts = [req.input] if isinstance(req.input, str) else req.input
            if not texts:
                raise HTTPException(status_code=400, detail="input must be non-empty")
            vectors = embedder.encode.remote(texts)
            meta = embedder.meta.remote()
            data = [EmbObj(index=i, embedding=v) for i, v in enumerate(vectors)]
            approx = sum(len(t.split()) for t in texts)
            return EmbResp(
                data=data,
                model=meta["model"],
                usage=EmbUsage(prompt_tokens=approx, total_tokens=approx),
            )

        return web

    # Note: @app.local_entrypoint() intentionally omitted. Modal 0.64 requires
    # local entrypoints to be module-scoped functions; the factory approach
    # here defines them as closures. `modal run modal_embedder.py` is nice
    # for one-off local smokes but is not part of the programmatic deploy
    # contract. Re-add as a module-level function if needed later.
    return app


# ── CLI-deploy compatibility ──────────────────────────────────────────────
# `modal deploy modal_embedder.py` picks up this module-level `app` symbol.
# Built from POLYMATH_* env vars at import time so existing shell workflows
# continue to work unchanged. The programmatic deployer path (Phase 22)
# calls build_app() directly with kwargs — it does NOT use this symbol.
app = build_app(
    app_name=os.getenv("POLYMATH_APP_NAME", "polymath-embedder"),
    model_id=os.getenv("POLYMATH_MODEL_ID", "Qwen/Qwen3-Embedding-0.6B"),
    gpu_tier=os.getenv("POLYMATH_GPU", "T4"),
    max_containers=int(os.getenv("POLYMATH_MAX_CONTAINERS", "10")),
    min_containers=int(os.getenv("POLYMATH_MIN_CONTAINERS", "0")),
    idle_timeout=int(os.getenv("POLYMATH_IDLE_TIMEOUT", "300")),
    concurrency=int(os.getenv("POLYMATH_CONCURRENCY", "4")),
    use_auth=bool(os.getenv("MODAL_USE_AUTH")),
)
