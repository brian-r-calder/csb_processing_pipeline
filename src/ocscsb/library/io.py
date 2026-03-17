from abc import ABC
from pathlib import Path
import datetime
import time
from contextlib import contextmanager
from enum import Enum

from smart_open import open as sopen

import boto3
import botocore.exceptions

# 72-hours TTL
DEFAULT_TTL_SEC = 259_200

class IOManager(ABC):
    def __init__(self, location: str):
        self.location = location

    def generate_resource_uri(self, object_name: str,
                              *,
                              sub_path: str | None = None) -> str | Path:
        ...

    def object_exists(self, object_name: str,
                      *,
                      ttl_sec: int = DEFAULT_TTL_SEC,
                      sub_path: str | None = None) -> bool:
        """
        Test if `object_name` exists and is newer than TTL seconds old
        Parameters
        ----------
        object_name
        ttl_sec : int
            Max age, at which, object is considered to be stale and therefore non-existent. Default 259,200 (72-hours)
        sub_path

        Returns
        -------
        True if `object_name` exists and is newer than TTL
        False if `object_name` exists and is older than TTL
        False if `object_name` does not exist
        """
        ...

    def open(self, object_name: str, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        """
        Open `object_name` for reading for writing.
        Parameters
        ----------
        object_name
        mode
        buffering
        encoding
        errors
        newline

        Returns
        -------
        A file-like object that can be read from or written to.
        """
        return sopen(self.generate_resource_uri(object_name),
                     mode=mode, buffering=buffering, encoding=encoding, errors=errors, newline=newline)


class IOManagerFile(IOManager):
    def __init__(self, location: str):
        super().__init__(location)
        self.location_path: Path = Path(self.location).absolute()

    def generate_resource_uri(self, object_name: str,
                              *,
                              sub_path: str | None = None) -> str | Path:
        object_parent = self.location_path
        if sub_path is not None:
            object_parent = object_parent / sub_path
        return object_parent / object_name

    def object_exists(self, object_name: str,
                      *,
                      ttl_sec: int = DEFAULT_TTL_SEC,
                      sub_path: str | None = None) -> bool:
        object_path: Path = self.generate_resource_uri(object_name, sub_path=sub_path)
        if not object_path.exists():
            return False
        # File exists, check its modification time to see if it is older than TTL
        curr_time = time.time()
        stat = object_path.stat()
        return stat.st_mtime > (curr_time - ttl_sec)


class IOManagerS3(IOManager):
    def __init__(self, location: str, client: boto3.client):
        super().__init__(location)
        self._client = client

    def generate_resource_uri(self, object_name: str,
                              *,
                              sub_path: str | None = None) -> str | Path:
        if sub_path is not None:
            return f"s3://{self.location}/{sub_path}/{object_name}"
        else:
            return f"s3://{self.location}/{object_name}"

    def object_exists(self, object_name: str,
                      *,
                      ttl_sec: int = DEFAULT_TTL_SEC,
                      sub_path: str | None = None) -> bool:
        object_path = object_name
        if sub_path is not None:
            object_path = f"{sub_path}/{object_name}"
        try:
            response = self._client.head_object(
                Bucket=self.location,
                Key=object_path
            )
            mtime = response['LastModified']
            curr_time = datetime.datetime.now(mtime.tzinfo)
            dt = datetime.timedelta(seconds=ttl_sec)
            print(response)
            return mtime > (curr_time - dt)
        except botocore.exceptions.ClientError as e:
            if 'An error occurred (404)' in str(e):
                return False
            raise e

    def open(self, object_name: str, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        return sopen(self.generate_resource_uri(object_name),
                     mode=mode, buffering=buffering, encoding=encoding, errors=errors, newline=newline,
                     transport_params={'client': self._client})


class StorageProviderType(Enum):
    LOCAL_FILE = 1
    S3 = 2

class File:
    def __init__(self, location: str, object_name: str, io_mgr: IOManager):
        self.location = location
        self.object_name = object_name
        self.io_mgr = io_mgr

    @classmethod
    def init(cls, location: str | Path, object_name: str, provider: StorageProviderType,
             **kwargs):
        location_str: str = str(location)
        match provider:
            case StorageProviderType.LOCAL_FILE:
                io_mgr: IOManager = IOManagerFile(location_str)
            case StorageProviderType.S3:
                io_mgr: IOManager = IOManagerS3(location_str,
                                                client=kwargs.get('client', None))
            case _:
                raise ValueError(f"Unable to create IO manager for unknown file provider type {provider.name}")
        return cls(location_str, object_name, io_mgr)

    def exists(self, *,
               ttl_sec: int = DEFAULT_TTL_SEC):
        return self.io_mgr.object_exists(self.object_name, ttl_sec=ttl_sec)

    @contextmanager
    def open(self, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        f = self.io_mgr.open(self.object_name,
                             mode=mode, buffering=buffering, encoding=encoding, errors=errors, newline=newline)
        try:
            yield f
        finally:
            f.close()

    def get_uri(self) -> str:
        return self.io_mgr.generate_resource_uri(self.object_name)


class StorageLocation:
    def __init__(self, location: str | Path, provider: StorageProviderType,
                 **kwargs):
        self.location = str(location)
        match provider:
            case StorageProviderType.LOCAL_FILE:
                self.io_mgr: IOManager = IOManagerFile(self.location)
            case StorageProviderType.S3:
                self.io_mgr: IOManager = IOManagerS3(self.location,
                                                     client=kwargs.get('client', None))
            case _:
                raise ValueError(f"Unable to create IO manager for unknown file provider type {provider.name}")

    def new_file(self, object_name: str) -> File:
        return File(self.location, object_name, self.io_mgr)

    def contains(self, object_name: str,
                 *,
                 ttl_sec: int = DEFAULT_TTL_SEC,
                 sub_path: str | None = None) -> bool:
        return self.io_mgr.object_exists(object_name, ttl_sec=ttl_sec, sub_path=sub_path)
