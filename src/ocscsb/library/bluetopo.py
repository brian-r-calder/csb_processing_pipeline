import os
import sqlite3
from dataclasses import dataclass

import boto3
from botocore import UNSIGNED
from botocore.client import Config

from nbs.bluetopo.core.build_vrt import connect_to_survey_registry
from nbs.bluetopo.core import fetch_tiles

BLUE_TOPO_BUCKET: str = 'noaa-ocs-nationalbathymetry-pds'

@dataclass
class TileDescriptor:
    tile_name: str
    subregion: str
    utm_zone: str
    bucket: str
    object: str
    geotiff_sha256_checksum: str


def list_tiles_s3(conn: sqlite3.Connection,
                  project_dir: str,
                  tile_prefix: str,
                  *,
                  bucket: str = BLUE_TOPO_BUCKET) -> tuple[list, list, list, list, list[TileDescriptor]]:
    """
    Adapted from nbs.bluetopo.core.fetch_tiles::download_tiles(), lines 501-588, removing bits not needed by the
    CSB processing workflow and specifically parts related to downloading files locally, which we don't need to do.

    Parameters
    ----------
    conn
    project_dir
    tile_prefix
    bucket

    Returns
    -------
    A tuple containing:
        existing_tiles: list[str]
        missing_tiles: list[str]
        tiles_found: list[str]
        tiles_not_found: list[str]
        tiles: list[TileDescriptor]
    """
    download_tile_list = fetch_tiles.all_db_tiles(conn)
    new_tile_list = [download_tile for download_tile in download_tile_list if
                     download_tile["geotiff_disk"] is None or download_tile["rat_disk"] is None]
    print("\nResolving fetch list...")
    # if tile_prefix != "Local":
    cred = {
        "aws_access_key_id": "",
        "aws_secret_access_key": "",
        "config": Config(signature_version=UNSIGNED),
    }
    client = boto3.client("s3", **cred)
    pageinator = client.get_paginator("list_objects_v2")
    existing_tiles: list = []
    missing_tiles: list = []
    tiles_found: list = []
    tiles_not_found: list = []
    tiles: list[TileDescriptor] = []
    for fields in download_tile_list:
        if fields["geotiff_disk"] and fields["rat_disk"]:
            if os.path.isfile(os.path.join(project_dir, fields["geotiff_disk"])) and os.path.isfile(os.path.join(project_dir, fields["rat_disk"])):
                if fields["geotiff_verified"] != "True" or fields["rat_verified"] != "True":
                    missing_tiles.append(fields["tilename"])
                else:
                    existing_tiles.append(fields["tilename"])
                    continue
            if os.path.isfile(os.path.join(project_dir, fields["geotiff_disk"])) is False or os.path.isfile(os.path.join(project_dir, fields["rat_disk"])) is False:
                missing_tiles.append(fields["tilename"])
        if "BlueTopo" in tile_prefix or "Modeling" in tile_prefix:
            tilename = fields["tilename"]
            pfx = tile_prefix + f"/{tilename}/"
            objs = pageinator.paginate(Bucket=bucket, Prefix=pfx).build_full_result()
            if len(objs) > 0:
                for object_name in objs["Contents"]:
                    source_name = object_name["Key"]
                    if source_name.lower().endswith('.tiff'):
                        tiles.append(
                            TileDescriptor(
                                tile_name=tilename,
                                subregion=fields["subregion"],
                                utm_zone=fields["utm"],
                                bucket=bucket,
                                object=source_name,
                                geotiff_sha256_checksum=fields["geotiff_sha256_checksum"]
                            )
                        )
                tiles_found.append(tilename)
            else:
                tiles_not_found.append(tilename)
        else:
            raise ValueError(f"Invalid tile prefix: {tile_prefix}")
    return existing_tiles, missing_tiles, tiles_found, tiles_not_found, tiles

def identify_tiles(project_dir: str,
                   desired_area_filename: str,
                   *,
                   data_source: str = 'Modeling',
                   geom_prefix: str = 'Test-and-Evaluation/Modeling/_Modeling_Tile_Scheme/Modeling_Tile_Scheme',
                   tile_prefix: str = 'Test-and-Evaluation/Modeling') -> tuple[list, list, list, list, dict]:
    """

    Parameters
    ----------
    project_dir
    desired_area_filename
    data_source
    geom_prefix
    tile_prefix

    Returns
    -------
    A tuple containing:
        existing_tiles: list
        missing_tiles: list
        tiles_found: list
        tiles_not_found: list
        tile_dict: dict
    """
    conn = connect_to_survey_registry(project_dir, data_source)
    geom_file = fetch_tiles.get_tessellation(conn, project_dir, geom_prefix, data_source)
    # Get tile list based on desired area
    if not os.path.isfile(desired_area_filename):
        raise ValueError(f"The geometry {desired_area_filename} for " "determining what to download does not exist.")
    tile_list = fetch_tiles.get_tile_list(desired_area_filename, geom_file)
    available_tile_count = fetch_tiles.insert_new(conn, tile_list)
    print(f"\nTracking {available_tile_count} available {data_source} tile(s) " f"discovered in a total of {len(tile_list)} intersected tile(s) " "with given polygon.")
    fetch_tiles.upsert_tiles(conn, project_dir, geom_file)
    # Query BlueTopo tiles from S3
    return list_tiles_s3(conn, project_dir, tile_prefix)
