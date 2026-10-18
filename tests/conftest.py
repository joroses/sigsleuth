import os
import pickle
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from filelock import FileLock

from recorded_sample import Sample, build_sample
from sigsleuth import db, terrain
from sigsleuth.api import create_app
from sigsleuth.config import Settings


@pytest.fixture(scope="session", autouse=True)
def no_terrain_tiles() -> Iterator[None]:
    """Tests never fetch terrain tiles: every fetch fails unless a test
    supplies its own."""

    def unavailable(key: terrain.TileKey) -> bytes:
        raise OSError("no terrain tiles in tests")

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(terrain, "fetch_tile", unavailable)
        yield


@pytest.fixture(scope="session")
def sample(tmp_path_factory: pytest.TempPathFactory) -> Sample:
    """The recorded sample, captured once per test run. Tests only read it.

    Under pytest-xdist the first worker captures it into the run's shared
    directory and the others wait for it."""
    if "PYTEST_XDIST_WORKER" not in os.environ:
        return build_sample(tmp_path_factory.mktemp("sample") / "history.db")
    shared = tmp_path_factory.getbasetemp().parent
    saved = shared / "sample.pickle"
    with FileLock(shared / "sample.lock"):
        if not saved.exists():
            saved.write_bytes(pickle.dumps(build_sample(shared / "sample.db")))
    sample: Sample = pickle.loads(saved.read_bytes())
    return sample


@pytest.fixture
def sample_client(sample: Sample, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """The API on the sample, with the clock stopped a minute after its last delivery."""
    monkeypatch.setattr(db, "fixed_now_ms", sample.now_ms)
    yield TestClient(create_app(Settings(Path(sample.path), default_preset="LongFast")))
