import sys
from pathlib import Path
import traceback
import click
from rich import print
import geopandas as gpd
import duckdb

from ocscsb import __version__ as version
from ocscsb.library.dcdb import ensure_grid_id_exists, csv_file_exists, process_tile
from ocscsb.library.database import (
    enable_spatial,
    db_unique_ids,
    ingest_geopackages,
    replace_depth_diff,
    apply_depth_offsets,
    gpkg_outliers_to_db,
    augment_db_for_transits,
    export_db_to_gpkg,
    query_by_bbox,
    count_outliers,
)
from ocscsb.library.analysis import (
    generate_offset_histograms,
    outlier_detect_gpkg,
    make_transits_by_id,
    plot_surface_diff_pmf,
    aggregate_points,
    plot_surface_diff,
)
from ocscsb.library.geotiff import (
    gpkgs_to_geotiffs,
    rasterize_geotiff,
    get_bbox_wgs84,
    sample_grid,
    diff_grid_to_geotiff
)

@click.version_option(version=version)
@click.group()
def cli():
    pass

@click.command()
@click.argument('input_shp', type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path))
@click.argument('output_dir', type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path))
@click.argument('email', type=str)
def scrape(input_shp: Path, output_dir: Path, email: str):
    '''Search the DCDB archive API for CSB files from AWS.

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


@click.command()
@click.argument('source_dir', type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path))
@click.argument('cleaned_dir', type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path))
@click.option('--geotiffs', type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
              default=Path('DEFAULT'),
              help='Directory for GeoTIFF output per GeoPackage')
@click.option('--plots', type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
              default=Path('DEFAULT'),
              help='Directory for plots of outliers')
@click.option('-v', '--verbose', type=bool, is_flag=True, default=False, help='Display verbose messages on execution status')
def outlier_detect(source_dir: Path, cleaned_dir: Path, geotiffs: Path, plots: Path, verbose: bool) -> None:
    '''Run outlier detection on a directory of GeoPackages.

    This command runs an iterative outlier detection algorithm over each GeoPackage file (.gpkg) in the
    SOURCE_DIR directory, writing the processed output to CLEANED_DIR with a prefix of "Processed_".
    '''
    options: dict = {
        'verbose': verbose,
    }
    if plots.name != 'DEFAULT':
        options['plot_dir'] = str(plots)

    n_processed: int = 0
    n_total: int = 0
    for filename in source_dir.glob('*.gpkg'):
        n_total += 1
        if outlier_detect_gpkg(filename, cleaned_dir / 'Processed_' / filename.name, **options):
            n_processed += 1
    if geotiffs.name != 'DEFAULT':
        gpkgs_to_geotiffs(cleaned_dir, geotiffs, **options)

    if verbose:
        print(f'[blue]Debug:[/] Processing {n_processed} GeoPackage files from {n_total}.')

@click.command()
@click.argument('db_file', type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path))
@click.argument('output_dir', type=click.Path(file_okay=False, dir_okay=True, path_type=Path))
@click.option('--geotiffs', type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
              default=Path('DEFAULT'),
              help='Directory for GeoTIFF output per GeoPackage')
@click.option('--plots', type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
              default=Path('DEFAULT'),
              help='Directory for plots of outliers')
@click.option('--maxgap', type=float, default=4.0, help='Maximum gap (hours) between points in a transit')
@click.option('--maxduration', type=float, default=7.0, help='Maximum duration (days) in transits')
@click.option('--resolution', type=float, default=8.0, help='GeoTIFF output resolution, if required')
@click.option('--epsg', type=int, default=4326, help='Set EPSG for output of GeoTIFFs')
@click.option('-v', '--verbose', type=bool, is_flag=True, default=False,
              help='Display verbose messages on execution status')
def export_transits(db_file: Path, output_dir: Path, geotiffs: Path, plots: Path,
                    maxgap: float, maxduration: float, resolution: float, epsg: int,
                    verbose: bool) -> None:
    '''Compute transits for unique ids, and output GeoPackages.

    This command computes transits from the observations in DB_FILE, and exports the transit as a
    separate GeoPackage in OUTPUT_DIR for further analysis, optionally generating plots and GeoTIFFs
    from the transits for diagnostic purposes.
    '''
    options: dict = {
        'verbose': verbose
    }
    if geotiffs.name != 'DEFAULT':
        options['geotiff_dir'] = geotiffs
        options['geotiff_res'] = resolution
    if plots.name != 'DEFAULT':
        options['plots_dir'] = plots
    
    with duckdb.connect(database=db_file.as_posix()) as con:
        enable_spatial(con)
        augment_db_for_transits(con, **options)
        unique_ids = db_unique_ids(con)
        for unique_id in unique_ids:
            make_transits_by_id(con, unique_id, output_dir, maxgap, maxduration, epsg, **options)

@click.command()
@click.argument('db_file', type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path))
@click.option('--gpkg',
              type=click.Path(file_okay=True, dir_okay=False, path_type=Path),
              default=Path('DEFAULT'),
              help='Set GeoPackage output file (default is same as db_file.gpkg)')
@click.option('--shapefile',
              type=click.Path(file_okay=True, dir_okay=False, path_type=Path),
              default=Path('DEFAULT'),
              help='Set output format to ShapeFile and set filename (default is GeoPackage)')
@click.option('--geotiff',
              type=click.Path(file_okay=True, dir_okay=False, path_type=Path),
              default=Path('DEFAULT'),
              help='Generate GeoTIFF from non-outliers with given name')
@click.option('--resolution',
              type=float, default=10.0,
              help='GeoTIFF output resolution, if required')
@click.option('--epsg',
              type=int, default=4326,
              help='Set EPSG for output')
@click.option('-v', '--verbose',
              type=bool, is_flag=True, default=False,
              help='Display verbose messages on execution status')
def export_db(db_file: Path, gpkg: Path, shapefile: Path, geotiff: Path, resolution: float, epsg: int, verbose: bool) -> None:
    '''Export DuckDB to GeoPackage, and optionally GeoTIFF.

    This command writes the points in the DuckDB DB_FILE as a GeoPackage in GPKG, projecting
    into a new EPSG zone if specified, and optionally writing a rasterized version of the
    depth observations as a GeoTIFF (with specified resolution) if required.
    '''
    options: dict = {
        'verbose': verbose,
        'epsg': epsg
    }
    if gpkg.name == 'DEFAULT':
        gpkg_name: Path = db_file.with_suffix('.gpkg')
    else:
        gpkg_name: Path = gpkg
    if shapefile.name != 'DEFAULT':
        if gpkg.name != 'DEFAULT':
            print('[red]Error:[/] You cannot specify both a GeoPackage and Shapefile name!')
            return
        options['shapefile'] = True
        gpkg_name: Path = shapefile

    gdf = export_db_to_gpkg(db_file, gpkg_name, **options)
    if geotiff.name != 'DEFAULT':
        rasterize_geotiff(gdf, geotiff, resolution)

@click.command()
@click.argument('db_file', type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path))
@click.argument('ref_file', type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path))
@click.argument('plot_dir', type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path))
@click.argument('geotiff', type=click.Path(file_okay=True, dir_okay=False, path_type=Path))
@click.option('--resolution', type=float, default=10.0, help='Output aggregate grid resolution (m)')
@click.option('-v', '--verbose',
              type=bool, is_flag=True, default=False,
              help='Display verbose messages on execution status')
def diff_viz(db_file: Path, ref_file: Path, plot_dir: Path, geotiff: Path, resolution: float, verbose: bool) -> None:
    '''Compute difference against reference, making plots.

    This command reads the corrected depths from the DuckDB DB_FILE and computes the difference
    in depth against the GeoTIFF REF_FILE (typically a BlueTopo tile), and generate a plot of the
    histogram of differences and color-coded plot of mean difference in PLOT_DIR (with well-known
    names for the files reflecting the inputs), and then saving the aggregated mean difference in
    a grid of given resolution as GEOTIFF.
    '''
    bbox = get_bbox_wgs84(ref_file)
    with duckdb.connect(database=db_file) as con:
        gdf: gpd.GeoDataFrame = query_by_bbox(con, bbox)
        if gdf.empty:
            print(f'[orange]Warning:[/] No CSB points found in reference bounding box - ignoring.')
            return
        gdf = sample_grid(ref_file, gdf)
        plot_surface_diff_pmf(gdf, plot_dir / f'histogram_{db_file.name}_{ref_file.name}.png')
        grid = aggregate_points(gdf, bbox['src_crs'], resolution)
        plot_surface_diff(grid,
                          plot_dir / f'difference_{db_file.name}_{ref_file.name}_{resolution}.png')
        diff_grid_to_geotiff(grid, geotiff, verbose=verbose)

@click.command()
@click.argument('db_file', type=click.Path(exists=True, file_okay=True, dir_okay=False, path_type=Path))
def outlier_counts(db_file: Path) -> None:
    '''Count the number of true/false outliers in db file.

    This command counts the number of outliers found in DB_FILE, the number not marked as outlier, and
    the total number of entries (which should be redundant, but you never know).
    '''

    with duckdb.connect(database=db_file) as con:
        outlier, inlier, total = count_outliers(con)
    
    print(f'[blue]Info:[/] {total} observations, {inlier} inliers, {outlier} outliers.')
    if inlier + outlier != total:
        print(f'[orange]Warning:[/] total count is not equal to sum of inliers and outliers!')

cli.add_command(scrape)
cli.add_command(ingest)
cli.add_command(offset_pmfs)
cli.add_command(apply_offsets)
cli.add_command(outlier_ingest)
cli.add_command(outlier_detect)
cli.add_command(export_transits)
cli.add_command(export_db)
cli.add_command(diff_viz)
cli.add_command(outlier_counts)