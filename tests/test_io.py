import os
from pathlib import Path
import time

import pytest

from .fixtures import temp_path, s3_client, garage_credentials, garage_layout

from smart_open import open as sopen

from ocscsb.library import io


@pytest.fixture(scope='function')
def dummy_file(temp_path):
    df: Path = temp_path / 'dummy.txt'
    df.touch()
    curr_time = time.time()
    # Create new file times 7 days in the past
    new_ftime = curr_time - (7 * 86_400)
    os.utime(df, times=(new_ftime, new_ftime))
    yield df

@pytest.fixture(scope='function')
def dummy_s3_object(s3_client):
    with sopen('s3://csb-dest/dummy.txt', 'wb',
               transport_params={'client': s3_client}) as fout:
        fout.write(b'hello world!')


def test_io_manager_file_object_exists(temp_path, dummy_file):
    io_mgr: io.IOManager = io.IOManagerFile(str(temp_path))
    assert not io_mgr.object_exists('dummy.txt')
    assert io_mgr.object_exists('dummy.txt', ttl_sec=8 * 86_400)


def test_io_manager_s3_object_exists(dummy_s3_object, s3_client):
    # Sleep for 1 second so that our TTL 1 second case passes as expected.
    time.sleep(1)
    io_mgr: io.IOManager = io.IOManagerS3('csb-dest', s3_client)
    assert io_mgr.object_exists('dummy.txt')
    assert not io_mgr.object_exists('dummy.txt', ttl_sec=1)
    assert not io_mgr.object_exists('doesnotexist.txt')
