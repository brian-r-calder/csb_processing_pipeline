import sys
from pathlib import Path
import traceback
import click
from rich import print
import geopandas as gpd

from ocscsb import __version__ as version
from ocscsb.library.dcdb import ensure_grid_id_exists, csv_file_exists, process_tile
from ocscsb.library.database import (
    ingest_geopackages,
    replace_depth_diff,
    apply_depth_offsets,
    gpkg_outliers_to_db
)
from ocscsb.library.analysis import generate_offset_histograms

@click.version_option(version=version)
@click.group()
def cli():
    pass

@click.command()
@click.argument('input_shp', type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path))
@click.argument('output_dir', type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path))
@click.argument('email', type=str)
def scrape(input_shp: Path, output_dir: Path, email: str):
    '''Search the DCDB archive API for CSB files from AWS

    This command queries the DCDB point-store API on AWS to find the CSV versions of the contributed CSB
    files for a given tile of data, as specified in the input Shapefile INPUT_SHP.  The CSVs retrieved
    are stored in OUTPUT_DIR.  The EMAIL specified is used for the API ordering information.
    '''
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

@click.command()
@click.argument('input_dir', type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path))
@click.argument('output_db', type=click.Path(file_okay=True, dir_okay=False, path_type=Path))
def ingest(input_dir: Path, output_db: Path) -> None:
    '''Read GeoPackage files of CSB data into a DuckDB file.

    This command reads all GeoPackage files in INPUT_DIR (files ending with .gpkg), and writes them into
    the 'csb' table in OUTPUT_DB, using DuckDB.  The INPUT_DIR must exist, although it can be empty; the
    OUTPUT_DB is created if it does not already exist.
    '''
    ingest_geopackages(input_dir, output_db)

@click.command()
@click.argument('input_db', type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path))
@click.argument('export_dir', type=click.Path(file_okay=False, dir_okay=False, path_type=Path))
@click.option('-v', '--verbose', type=bool, is_flag=True, default=False, help='Display verbose messages on execution status')
def offset_pmfs(input_db: Path, export_dir: Path, verbose: bool) -> None:
    '''Generate plots for vertical offset histograms.

    This command reads all of the observations in INPUT_DB and generates a histogram of the offset values
    between the observation and the reference depths for each unique id in the database.  The EXPORT_DIR
    is constructed if required, and is populated with CSV files for the source differences, and PNGs for
    the plotted histograms.
    '''
    if export_dir.exists() and not export_dir.is_dir():
        raise ValueError(f'Entity {export_dir.as_posix()} is not a directory')
    if not export_dir.exists():
        export_dir.mkdir(parents=True, exist_ok=True)

    # TODO: the reference code attempts to recompute the 'diff' column on the database if
    # it exists, using "the correct calculation"; it's not clear why the right calculation
    # isn't used in the first place!  Keep this in place for now, and check with ARK.
    replace_depth_diff(input_db, verbose=verbose)

    extracted, plotted = generate_offset_histograms(input_db, export_dir, verbose=verbose)
    if verbose:
        print(f'[blue]Info:[/] Extracted {extracted} unique IDs, plotted {plotted} histograms.')

@click.command()
@click.argument('input_db', type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path))
@click.option('-v', '--verbose', type=bool, is_flag=True, default=False, help='Display verbose messages on execution status')
def apply_offsets(input_db: Path, verbose: bool) -> None:
    '''Apply best offsets to depths in a DuckDB database.

    This deletes the 'depth_mod' and uncertainty columns in INPUT_DB (a DuckDB database), and recomputes them
    based on the average difference in the database for each unique ID.  The uncertainty is then recomputed
    based on the assumption of CATZOC C as a model for worst-case (but conservative) uncertainty.
    '''
    apply_depth_offsets(input_db, verbose=verbose)

@click.command()
@click.argument('input_dir', type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path))
@click.argument('db_file', type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path))
@click.option('-v', '--verbose', type=bool, is_flag=True, default=False, help='Display verbose messages on execution status')
def outlier_ingest(input_dir: Path, db_file: Path, verbose: bool) -> None:
    '''Transfer GeoPackage outlier flags to DuckDB.

    This reads the outlier flags in all GeoPackage files (*.gpkg) in INPUT_DIR and transfers them to the
    corresponding entries in the DB_FILE (DuckDB database).
    '''
    gpkg_outliers_to_db(input_dir, db_file, verbose=verbose)

cli.add_command(scrape)
cli.add_command(ingest)
cli.add_command(offset_pmfs)
cli.add_command(apply_offsets)
cli.add_command(outlier_ingest)
