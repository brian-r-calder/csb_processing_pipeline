from .fixtures import garage_layout, s3_client, garage_credentials


def test_smoke(garage_layout):
    assert 1 == 1

def test_smoke(s3_client):
    assert s3_client is not None
