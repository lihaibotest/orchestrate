from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import unittest


ROOT = Path(__file__).parents[2]
sys.path.insert(0, str(ROOT / "orchestrator" / "backend"))
sys.path.insert(0, str(ROOT))

from adapters.fake_artifact import FakeMinioClient, InMemoryArtifactRepository  # noqa: E402
from adapters.minio_artifact import MinioArtifactAdapter  # noqa: E402
from api.artifacts import create_artifact_router  # noqa: E402
from domain.artifact import (  # noqa: E402
    ArtifactNotFound,
    ArtifactPutRequest,
    ExpectedArtifactOutput,
    ProducedArtifact,
)


class ArtifactManagerFixture:
    def __init__(self) -> None:
        self.client = FakeMinioClient()
        self.repository = InMemoryArtifactRepository()
        self.manager = MinioArtifactAdapter(
            self.client, self.repository, bucket="artifacts"
        )
        self.expected = ExpectedArtifactOutput(
            "report",
            "FILE",
            "application/json",
            "schema://report/v1",
            {
                "type": "object",
                "required": ["title"],
                "properties": {"title": {"type": "string"}},
                "additionalProperties": False,
            },
            min_size_bytes=2,
            max_size_bytes=1024,
        )

    def request(
        self,
        *,
        tenant: str = "tenant-a",
        execution: str = "execution-a",
        mode: str = "PRODUCTION",
        attempt: int = 1,
        content: bytes = b'{"title":"ok"}',
        media_type: str = "application/json",
        schema_ref: str | None = "schema://report/v1",
        expected: ExpectedArtifactOutput | None = None,
    ) -> ArtifactPutRequest:
        return ArtifactPutRequest(
            tenant,
            "workflow-a",
            1,
            execution,
            mode,
            "node-a",
            attempt,
            ProducedArtifact("report", content, media_type, schema_ref),
            expected or self.expected,
        )


class ArtifactManagerTests(unittest.TestCase):
    def test_put_get_exists_validate_and_idempotent_lost_response(self) -> None:
        fixture = ArtifactManagerFixture()
        fixture.client.lose_put_response_once = True
        first = fixture.manager.put_artifact(fixture.request())
        replay = fixture.manager.put_artifact(fixture.request())
        self.assertEqual(first.artifact_id, replay.artifact_id)
        self.assertEqual(len(fixture.client.objects), 1)
        self.assertTrue(fixture.manager.check_exists("tenant-a", first.artifact_id))
        validation = fixture.manager.validate_artifact(
            "tenant-a", first.artifact_id, fixture.expected
        )
        self.assertTrue(validation.passed)
        loaded = fixture.manager.get_artifact_metadata("tenant-a", first.artifact_id)
        self.assertEqual((loaded.status, loaded.validation_status), ("VALIDATED", "PASS"))
        self.assertNotIn(b'{"title":"ok"}', repr(loaded).encode())

    def test_validation_missing_wrong_media_hash_schema_and_schema_ref(self) -> None:
        cases = []

        missing = ArtifactManagerFixture()
        identity = missing.manager.put_artifact(missing.request())
        key = next(iter(missing.client.objects))[1]
        missing.client.remove_object("artifacts", key)
        cases.append((missing, identity.artifact_id, "ARTIFACT_MISSING"))

        wrong_media = ArtifactManagerFixture()
        identity = wrong_media.manager.put_artifact(
            wrong_media.request(media_type="text/plain")
        )
        cases.append((wrong_media, identity.artifact_id, "ARTIFACT_MEDIA_TYPE_MISMATCH"))

        wrong_hash = ArtifactManagerFixture()
        identity = wrong_hash.manager.put_artifact(wrong_hash.request())
        key = next(iter(wrong_hash.client.objects))[1]
        wrong_hash.client.corrupt("artifacts", key, b'{"title":"no"}')
        cases.append((wrong_hash, identity.artifact_id, "ARTIFACT_HASH_MISMATCH"))

        wrong_schema = ArtifactManagerFixture()
        identity = wrong_schema.manager.put_artifact(
            wrong_schema.request(content=b'{"wrong":"value"}')
        )
        cases.append((wrong_schema, identity.artifact_id, "ARTIFACT_SCHEMA_MISMATCH"))

        wrong_ref = ArtifactManagerFixture()
        identity = wrong_ref.manager.put_artifact(
            wrong_ref.request(schema_ref="schema://other/v1")
        )
        cases.append((wrong_ref, identity.artifact_id, "ARTIFACT_SCHEMA_MISMATCH"))

        wrong_size = ArtifactManagerFixture()
        too_small = ExpectedArtifactOutput(
            "report", media_type="application/json", min_size_bytes=100
        )
        identity = wrong_size.manager.put_artifact(
            wrong_size.request(expected=too_small, schema_ref=None)
        )
        wrong_size.expected = too_small
        cases.append((wrong_size, identity.artifact_id, "ARTIFACT_SIZE_MISMATCH"))

        for fixture, artifact_id, error_code in cases:
            with self.subTest(error_code=error_code):
                result = fixture.manager.validate_artifact(
                    "tenant-a", artifact_id, fixture.expected
                )
                self.assertFalse(result.passed)
                self.assertIn(error_code, {item.error_code for item in result.checks})
                stored = fixture.manager.get_artifact_metadata("tenant-a", artifact_id)
                self.assertEqual((stored.status, stored.validation_status), ("INVALID", "FAIL"))

    def test_domain_application_and_port_do_not_import_minio_sdk(self) -> None:
        for folder in ("domain", "application", "ports"):
            for source in (ROOT / "orchestrator" / "backend" / folder).glob("*.py"):
                text = source.read_text(encoding="utf-8")
                self.assertNotIn("from minio", text, source)
                self.assertNotIn("import minio", text, source)

    def test_namespace_mode_security_delete_and_tenant_isolation(self) -> None:
        fixture = ArtifactManagerFixture()
        production = fixture.manager.put_artifact(fixture.request())
        self_test = fixture.manager.put_artifact(
            fixture.request(execution="execution-self-test", mode="SELF_TEST")
        )
        keys = sorted(key for _bucket, key in fixture.client.objects)
        self.assertTrue(any("/production/" in f"/{key}/" for key in keys))
        self.assertTrue(any("/self_test/" in f"/{key}/" for key in keys))
        with self.assertRaises(ArtifactNotFound):
            fixture.manager.get_artifact_metadata("tenant-b", production.artifact_id)
        self.assertFalse(fixture.manager.check_exists("tenant-b", production.artifact_id))
        fixture.manager.delete_artifact("tenant-a", self_test.artifact_id)
        self.assertFalse(fixture.manager.check_exists("tenant-a", self_test.artifact_id))
        with self.assertRaises(ValueError):
            ProducedArtifact(
                "report",
                b"{}",
                "application/json",
                metadata={"api_key": "plaintext"},
            )


async def _request(app, artifact_id: str, tenant: str):  # type: ignore[no-untyped-def]
    import httpx

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(
            f"/api/v1/artifacts/{artifact_id}", headers={"X-Tenant-Id": tenant}
        )


class ArtifactApiTests(unittest.TestCase):
    def test_metadata_endpoint_never_returns_object_content(self) -> None:
        from fastapi import FastAPI

        fixture = ArtifactManagerFixture()
        identity = fixture.manager.put_artifact(fixture.request())
        fixture.manager.validate_artifact("tenant-a", identity.artifact_id, fixture.expected)
        app = FastAPI()
        app.include_router(create_artifact_router(fixture.manager))
        response = asyncio.run(_request(app, identity.artifact_id, "tenant-a"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["artifact_id"], identity.artifact_id)
        self.assertNotIn("content", body)
        denied = asyncio.run(_request(app, identity.artifact_id, "tenant-b"))
        self.assertEqual(denied.status_code, 404)


if __name__ == "__main__":
    unittest.main()
