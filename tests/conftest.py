import pytest

from scout.db import Base, make_engine, make_session_factory
from scout.repository import Repository


@pytest.fixture
def repo(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    repository = Repository(make_session_factory(engine))
    repository.seed()
    yield repository
    engine.dispose()
