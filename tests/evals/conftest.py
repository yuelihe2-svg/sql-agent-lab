import pytest

from evals.shopbench_v1.seed import generate_all

@pytest.fixture(scope="session")
def shopbench(tmp_path_factory):
    directory = tmp_path_factory.mktemp("shopbench")
    manifest = generate_all(directory)
    return directory, manifest
