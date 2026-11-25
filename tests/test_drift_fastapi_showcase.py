from __future__ import annotations

import anyio
import drift
import httpx
import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel


class PublicUser(BaseModel):
    id: int
    name: str
    email: str


class InternalUser(PublicUser):
    hashed_password: str
    secret_token: str


class ItemIn(BaseModel):
    name: str
    price: float


def _build_validation_app() -> FastAPI:
    app = FastAPI()

    @app.get("/users", response_model=PublicUser)
    def get_user(include_secret: bool = True) -> InternalUser:
        return InternalUser(
            id=1,
            name="Alice",
            email="alice@example.com",
            hashed_password="bcrypt$secret$value" if include_secret else "",
            secret_token="tok_live_999" if include_secret else "",
        )

    @app.post("/items")
    def create_item(item: ItemIn) -> dict[str, object]:
        return {"name": item.name, "price": item.price}

    @app.get("/async-users")
    async def get_async_user(user_id: int) -> dict[str, object]:
        await anyio.sleep(0)
        return {
            "id": user_id,
            "name": "Bob",
            "email": "bob@example.com",
            "diagnostic_blob": "D" * 1500,
        }

    return app


def _build_depends_app(override: bool) -> FastAPI:
    app = FastAPI()

    async def get_envelope_meta(
        x_role: str = Header(default="reader"),
    ) -> dict[str, object]:
        if x_role == "blocked":
            raise HTTPException(status_code=403, detail="forbidden")
        return {
            "$t": "audit.envelope",
            "$meta": {"version": 2, "source": "depends"},
            "role": x_role,
        }

    async def override_envelope_meta(
        x_role: str = Header(default="admin"),
    ) -> dict[str, object]:
        return {
            "$t": "audit.override",
            "$meta": {"version": 3, "source": "override"},
            "role": x_role,
        }

    if override:
        app.dependency_overrides[get_envelope_meta] = override_envelope_meta

    @app.get("/audit")
    async def audit_route(
        envelope: dict[str, object] = Depends(get_envelope_meta),
    ) -> dict[str, object]:
        return {"envelope": envelope}

    return app


def test_response_model_strips_secret() -> None:
    app = _build_validation_app()
    client = TestClient(app)

    def _run_strips_secret(d: drift.DriftContext) -> None:
        endpoint = d.when("response-model-strips-secret", "endpoint")
        include_secret = d.when("response-model-strips-secret", "include_secret")
        resp = client.get(endpoint, params={"include_secret": str(include_secret).lower()})
        payload = {"status": resp.status_code, "body": resp.json()}
        d.check_projection("response-model-strips-secret", payload)

    drift.run_drift_case(
        "fastapi-response-validation",
        "tests.test_drift_fastapi_showcase#test_response_model_strips_secret",
        ["response-model-strips-secret"],
        _run_strips_secret,
        level="http",
        strict_when=True,
    )


def test_validation_error_422_shape() -> None:
    app = _build_validation_app()
    client = TestClient(app)

    def _run_validation_422(d: drift.DriftContext) -> None:
        endpoint = d.when("validation-error-422-shape", "endpoint")
        price = d.when("validation-error-422-shape", "price")
        resp = client.post(endpoint, json={"name": "widget", "price": price})
        body = resp.json()
        # Attach a large unreferenced diagnostic field to exercise projected observation extraction.
        body["diagnostic_blob"] = "X" * 1500
        payload = {"status": resp.status_code, "body": body}
        d.check_projection("validation-error-422-shape", payload)

    drift.run_drift_case(
        "fastapi-response-validation",
        "tests.test_drift_fastapi_showcase#test_validation_error_422_shape",
        ["validation-error-422-shape"],
        _run_validation_422,
        level="http",
        strict_when=True,
    )


def test_async_route_projected_ok() -> None:
    app = _build_validation_app()

    async def _fetch(endpoint: str, user_id: int) -> dict[str, object]:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get(endpoint, params={"user_id": user_id})
            return {"status": resp.status_code, "body": resp.json()}

    def _run_async_projected(d: drift.DriftContext) -> None:
        endpoint = d.when("async-route-projected-ok", "endpoint")
        user_id = d.when("async-route-projected-ok", "user_id")
        payload = anyio.run(_fetch, endpoint, user_id)
        d.check_projection("async-route-projected-ok", payload)

    drift.run_drift_case(
        "fastapi-response-validation",
        "tests.test_drift_fastapi_showcase#test_async_route_projected_ok",
        ["async-route-projected-ok"],
        _run_async_projected,
        level="http",
        strict_when=True,
    )


@pytest.mark.parametrize(
    "case_id",
    [
        "depends-injects-envelope",
        "depends-override-applies",
    ],
)
def test_depends_envelope_literal_meta(case_id: str) -> None:
    def _run_depends_envelope(d: drift.DriftContext) -> None:
        role = d.when(case_id, "header_role")
        override = d.when(case_id, "override")
        app = _build_depends_app(override=override)
        client = TestClient(app)
        resp = client.get("/audit", headers={"X-Role": role})
        d.check(case_id, {"status": resp.status_code, "envelope": resp.json()["envelope"]})

    drift.run_drift_case(
        "fastapi-depends-literal-meta",
        f"tests.test_drift_fastapi_showcase#test_depends_envelope_literal_meta[{case_id}]",
        [case_id],
        _run_depends_envelope,
        level="http",
        strict_when=True,
    )


def test_depends_forbids_unauthorized() -> None:
    def _run_depends_forbid(d: drift.DriftContext) -> None:
        role = d.when("depends-forbids-unauthorized", "header_role")
        override = d.when("depends-forbids-unauthorized", "override")
        app = _build_depends_app(override=override)
        client = TestClient(app)
        resp = client.get("/audit", headers={"X-Role": role})
        d.check(
            "depends-forbids-unauthorized",
            {"status": resp.status_code, "detail": resp.json()["detail"]},
        )

    drift.run_drift_case(
        "fastapi-depends-literal-meta",
        "tests.test_drift_fastapi_showcase#test_depends_forbids_unauthorized",
        ["depends-forbids-unauthorized"],
        _run_depends_forbid,
        level="http",
        strict_when=True,
    )
