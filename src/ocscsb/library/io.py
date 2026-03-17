from abc import ABC
from pathlib import Path
import datetime
import time
from contextlib import contextmanager
from typing import IO, AnyStr, Any, Generator

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

    @contextmanager
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
        f = sopen(self.generate_resource_uri(object_name),
                  mode=mode, buffering=buffering, encoding=encoding, errors=errors, newline=newline)
        try:
            yield f
        finally:
            f.close()


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

    @contextmanager
    def open(self, object_name: str, mode='r', buffering=-1, encoding=None, errors=None, newline=None):
        f = sopen(self.generate_resource_uri(object_name),
                  mode=mode, buffering=buffering, encoding=encoding, errors=errors, newline=newline,
                  transport_params={'client': self._client})
        try:
            yield f
        finally:
            f.close()
