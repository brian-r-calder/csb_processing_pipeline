import os
from pathlib import Path
import time

import pytest

from .fixtures import temp_path, s3_client, garage_credentials, garage_layout

from smart_open import open as sopen

from ocscsb.library import io

DUMMY_FILE_NAME: str = 'dummy.txt'
NEW_FILE_NAME: str = 'new.txt'
HELLO_WORLD: str = 'Hello, World!'
HELLO_WORLD_BYTES: bytes = b'Hello, World!'


@pytest.fixture(scope='function')
def dummy_file(temp_path):
    df: Path = temp_path / DUMMY_FILE_NAME
    df.write_text(HELLO_WORLD)
    curr_time = time.time()
    # Set access and mod time times 7 days in the past
    new_ftime = curr_time - (7 * 86_400)
    os.utime(df, times=(new_ftime, new_ftime))
    yield df

@pytest.fixture(scope='function')
def dummy_s3_object(s3_client):
    with sopen(f"s3://{s3_client['bucket']}/{DUMMY_FILE_NAME}", 'wb',
               transport_params={'client': s3_client['client']}) as fout:
        fout.write(HELLO_WORLD_BYTES)


def test_local_file_object_exists(temp_path, dummy_file):
    dummy_file: io.File = io.File(temp_path, DUMMY_FILE_NAME, io.FileProviderType.LOCAL_FILE)
    assert not dummy_file.exists()
    assert dummy_file.exists(ttl_sec=8 * 86_400)
    with dummy_file.open() as f:
        line = f.readline()
        assert HELLO_WORLD == line

def test_local_file_write(temp_path):
    new_file: io.File = io.File(temp_path, NEW_FILE_NAME, io.FileProviderType.LOCAL_FILE)
    with new_file.open(mode='w') as f_out:
        f_out.writelines(HELLO_WORLD)
    with new_file.open() as f_in:
        assert HELLO_WORLD == f_in.readline()

def test_s3_object_exists(dummy_s3_object, s3_client):
    # Sleep for 1 second so that our TTL 1 second case passes as expected.
    time.sleep(1)
    dummy_file: io.File = io.File(s3_client['bucket'], DUMMY_FILE_NAME, io.FileProviderType.S3,
                                  client=s3_client['client'])
    assert dummy_file.exists()
    assert not dummy_file.exists(ttl_sec=1)
    with dummy_file.open() as f:
        line = f.readline()
        assert HELLO_WORLD == line

def test_s3_write(s3_client):
    new_file: io.File = io.File(s3_client['bucket'], NEW_FILE_NAME, io.FileProviderType.S3,
                                client=s3_client['client'])
    with new_file.open(mode='w') as f_out:
        f_out.writelines(HELLO_WORLD)
    with new_file.open() as f_in:
        assert HELLO_WORLD == f_in.readline()
