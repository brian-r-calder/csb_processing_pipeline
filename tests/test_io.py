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
    file_abs: str = os.path.join(temp_path, DUMMY_FILE_NAME)
    dummy_file: io.File = io.File.init(file_abs)
    assert dummy_file.exists()
    assert not dummy_file.exists(ttl_sec=io.DEFAULT_TTL_SEC)
    assert dummy_file.exists(ttl_sec=8 * 86_400)
    # Test io.File.open() as a contextmanager
    with dummy_file.open() as f:
        line = f.readline()
        assert HELLO_WORLD == line

    # Test io.File.open() directly
    def reader(fd) -> str:
        return fd.read()

    assert HELLO_WORLD == reader(dummy_file.open())


def test_local_file_write(temp_path):
    location: io.StorageLocation = io.StorageLocation(temp_path, 'LOCAL_FILE')
    assert not location.contains(NEW_FILE_NAME)
    new_file: io.File = location.new_file(NEW_FILE_NAME)
    with new_file.open(mode='w') as f_out:
        f_out.writelines(HELLO_WORLD)
    with new_file.open() as f_in:
        assert HELLO_WORLD == f_in.readline()

def test_local_file_write_subdir(temp_path):
    # Make sure parent directory preparation works
    location: io.StorageLocation = io.StorageLocation(temp_path, 'LOCAL_FILE')
    fpath_rel = 'newsubdir/file.txt'
    file: io.File = location.new_file(fpath_rel)
    assert not file.exists()
    with file.open(mode='w') as f:
        f.writelines(HELLO_WORLD)
    with file.open() as f:
        assert HELLO_WORLD == f.readline()

def test_s3_object_exists(dummy_s3_object, s3_client):
    # Sleep for 1 second so that our TTL 1 second case passes as expected.
    time.sleep(1)
    dummy_file: io.File = io.File.init(s3_client['bucket'],
                                       object_name=DUMMY_FILE_NAME,
                                       provider='S3',
                                       client=s3_client['client'])
    assert dummy_file.exists()
    assert not dummy_file.exists(ttl_sec=1)
    with dummy_file.open() as f:
        line = f.readline()
        assert HELLO_WORLD == line

def test_s3_write(s3_client):
    location: io.StorageLocation = io.StorageLocation(s3_client['bucket'], 's3',
                                                      client=s3_client['client'])
    assert not location.contains(NEW_FILE_NAME)
    new_file: io.File = location.new_file(NEW_FILE_NAME)
    with new_file.open(mode='w') as f_out:
        f_out.writelines(HELLO_WORLD)
    with new_file.open() as f_in:
        assert HELLO_WORLD == f_in.readline()

def test_s3_write_subdir(s3_client):
    # Make sure parent directory preparation works
    location: io.StorageLocation = io.StorageLocation(s3_client['bucket'], 'S3',
                                                      client=s3_client['client'])
    fpath_rel = 'newsubdir/file.txt'
    file: io.File = location.new_file(fpath_rel)
    assert not file.exists()
    with file.open(mode='w') as f:
        f.writelines(HELLO_WORLD)
    with file.open() as f:
        assert HELLO_WORLD == f.readline()

def test_local_list_objects(temp_path):
    # Create some files
    (temp_path / "test1.txt").write_text("content1")
    (temp_path / "test2.txt").write_text("content2")
    (temp_path / "other.dat").write_text("other")

    sub = temp_path / "sub"
    sub.mkdir()
    (sub / "sub1.txt").write_text("subcontent")

    location = io.StorageLocation(temp_path, io.StorageProviderType.LOCAL_FILE)

    # List all
    files = location.list_files()
    names = [f.object_name for f in files]
    assert "test1.txt" in names
    assert "test2.txt" in names
    assert "other.dat" in names
    assert len(names) == 3

    # Verify stem
    expected_stems = ['test1', 'test2', 'other']
    expected_suffixes = ['.txt', '.txt', '.dat']
    for i, f in enumerate(files):
        assert f.get_stem() == expected_stems[i]
        assert f.get_suffix() == expected_suffixes[i]

    # List with suffix
    files = location.list_files(suffix=".txt")
    names = [f.object_name for f in files]
    assert "test1.txt" in names
    assert "test2.txt" in names
    assert "other.dat" not in names
    assert len(names) == 2

    # List sub_path
    files = location.list_files(sub_path="sub")
    names = [f.object_name for f in files]
    assert "sub/sub1.txt" in names
    assert len(names) == 1

def test_local_delete(temp_path):
    f_path = temp_path / "delete_me.txt"
    f_path.write_text("bye")

    # Test conversion from Path to io.File
    f_file: io.File = io.File.from_path(f_path)
    assert f_file.location == str(temp_path)
    assert f_file.object_name == "delete_me.txt"
    assert f_file.exists()

    location = io.StorageLocation(temp_path, io.StorageProviderType.LOCAL_FILE)
    assert location.contains("delete_me.txt")

    location.delete_file("delete_me.txt")
    assert not location.contains("delete_me.txt")
    assert not f_path.exists()

def test_local_delete_all(temp_path):
    sub = temp_path / "rm_me"
    sub.mkdir()
    (sub / "inner.txt").write_text("gone")

    location = io.StorageLocation(temp_path, io.StorageProviderType.LOCAL_FILE)
    assert (sub / "inner.txt").exists()

    location.delete_all("rm_me")
    assert not sub.exists()

def test_get_uri(temp_path):
    location = io.StorageLocation(temp_path, io.StorageProviderType.LOCAL_FILE)

    # Location URI
    assert location.get_uri() == str(temp_path.absolute())

    # Sub-path URI
    assert location.get_uri(sub_path="sub") == str((temp_path / "sub").absolute())

    # File URI
    assert location.get_uri("file.txt") == str((temp_path / "file.txt").absolute())
    # As an actual io.File
    eff: io.File = location.new_file("file.txt")
    assert eff.get_uri() == location.get_uri("file.txt")
    assert eff.get_gdal_vsi_path() == location.get_uri("file.txt")

    # File with sub-path URI
    assert location.get_uri("file.txt", sub_path="sub") == str((temp_path / "sub" / "file.txt").absolute())
    eff: io.File = location.new_file("sub/file.txt")
    assert eff.get_uri() == location.get_uri("file.txt", sub_path="sub")
    assert eff.get_gdal_vsi_path() == location.get_uri("file.txt", sub_path="sub")

def test_s3_list_objects(s3_client):
    client = s3_client['client']
    bucket = s3_client['bucket']

    # Create some objects
    client.put_object(Bucket=bucket, Key="test1.txt", Body=b"content1")
    client.put_object(Bucket=bucket, Key="test2.txt", Body=b"content2")
    client.put_object(Bucket=bucket, Key="other.dat", Body=b"other")
    client.put_object(Bucket=bucket, Key="sub/sub1.txt", Body=b"subcontent")

    location = io.StorageLocation(bucket, io.StorageProviderType.S3, client=client)

    # List all
    files = location.list_files()
    names = [f.object_name for f in files]
    assert "test1.txt" in names
    assert "test2.txt" in names
    assert "other.dat" in names
    assert "sub/sub1.txt" in names
    assert len(names) == 4

    # Verify stem
    for i, f in enumerate(files):
        assert f.get_stem() == f.object_name.split('/')[-1].split('.')[-2]
        assert f.get_suffix() == f".{f.object_name.split('/')[-1].split('.')[-1]}"

    # List with suffix
    files = location.list_files(suffix=".txt")
    names = [f.object_name for f in files]
    assert "test1.txt" in names
    assert "test2.txt" in names
    assert "sub/sub1.txt" in names
    assert "other.dat" not in names
    assert len(names) == 3

    # List sub_path
    files = location.list_files(sub_path="sub")
    names = [f.object_name for f in files]
    assert "sub/sub1.txt" in names
    assert len(names) == 1

def test_s3_delete(s3_client):
    client = s3_client['client']
    bucket = s3_client['bucket']

    client.put_object(Bucket=bucket, Key="delete_me.txt", Body=b"bye")

    location = io.StorageLocation(bucket, io.StorageProviderType.S3, client=client)
    assert location.contains("delete_me.txt")

    location.delete_file("delete_me.txt")
    assert not location.contains("delete_me.txt")

def test_s3_delete_all(s3_client):
    client = s3_client['client']
    bucket = s3_client['bucket']

    client.put_object(Bucket=bucket, Key="rm_me/inner1.txt", Body=b"gone")
    client.put_object(Bucket=bucket, Key="rm_me/inner2.txt", Body=b"gone")

    location = io.StorageLocation(bucket, io.StorageProviderType.S3, client=client)
    assert location.contains("rm_me/inner1.txt")
    assert location.contains("rm_me/inner2.txt")

    location.delete_all("rm_me")
    assert not location.contains("rm_me/inner1.txt")
    assert not location.contains("rm_me/inner2.txt")

def test_s3_get_uri(s3_client):
    client = s3_client['client']
    bucket = s3_client['bucket']
    location = io.StorageLocation(bucket, io.StorageProviderType.S3, client=client)

    # Location URI
    assert location.get_uri() == f"s3://{bucket}"

    # Sub-path URI
    assert location.get_uri(sub_path="sub") == f"s3://{bucket}/sub"

    # File URI
    assert location.get_uri("file.txt") == f"s3://{bucket}/file.txt"
    # As an actual io.File
    eff: io.File = location.new_file("file.txt")
    assert eff.get_uri() == location.get_uri("file.txt")
    assert eff.get_gdal_vsi_path() == f"/vsis3/{bucket}/file.txt"

    # File with sub-path URI
    assert location.get_uri("file.txt", sub_path="sub") == f"s3://{bucket}/sub/file.txt"
    eff: io.File = location.new_file("sub/file.txt")
    assert eff.get_uri() == location.get_uri("file.txt", sub_path="sub")
    assert eff.get_gdal_vsi_path() == f"/vsis3/{bucket}/sub/file.txt"
