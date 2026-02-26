import sys
from pathlib import Path
import traceback
import click
from rich import print
import geopandas as gpd

from ocscsb import __version__ as version
from ocscsb.library.dcdb import ensure_grid_id_exists, csv_file_exists, process_tile

@click.version_option(version=version)
@click.group()
def cli():
    pass

@click.command()
@click.argument('input_shp', type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path))
@click.argument('output_dir', type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path))
@click.argument('email', type=str)
def scrape(input_shp: Path, output_dir: Path, email: str):
    gdf: gpd.GeoDataFrame = gpd.read_file(input_shp).to_crs(epsg=4326)

    # Ensure GRID_ID exists and is populated
    gdf = ensure_grid_id_exists(gdf)

    # Iterate through each polygon in the GeoDataFrame
    for _, row in gdf.iterrows():
        polygon = row['geometry']
        tile_name = row['GRID_ID']
        
        # Check if the CSV file for the current tile already exists
        if csv_file_exists(tile_name, output_dir):
            print(f"[orange]Warning:[/] CSV file for GRID_ID {tile_name} already exists. Skipping download.")
            continue
        
        minx, miny, maxx, maxy = polygon.bounds
        bbox = f'{minx},{miny},{maxx},{maxy}'

        # Now call process_tile for each tile
        try:
            process_tile(bbox, email, tile_name, output_dir)
        except Exception as e:
            sys.exit(traceback.format_exc())

cli.add_command(scrape)
