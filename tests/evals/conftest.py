from pathlib import Path

import pytest

from evals.shopbench_v1.catalog import materialize
from evals.shopbench_v1.seed import generate_all

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="session", autouse=True)
def shopbench_cases():
    materialize()


@pytest.fixture(scope="session")
def shopbench(tmp_path_factory):
    directory = tmp_path_factory.mktemp("shopbench")
    manifest = generate_all(directory)
    return directory, manifest
