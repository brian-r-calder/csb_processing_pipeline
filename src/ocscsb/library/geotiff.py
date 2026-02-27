from pathlib import Path
import numpy as np
import geopandas as gpd
from shapely.geometry import Point
import rasterio
from rasterio.transform import from_origin
from traceback import format_exc
from rich import print

def create_geotiff(gdf: gpd.GeoDataFrame, filename: Path, resolution: float = 8.0):
    try:
        bounds = gdf.total_bounds
        x_min, y_min, x_max, y_max = bounds

        if x_max == x_min or y_max == y_min:
            raise ValueError("Invalid geographic bounds. All points may be identical or too close.")

        x_res = int((x_max - x_min) / resolution)
        y_res = int((y_max - y_min) / resolution)

        transform = from_origin(x_min, y_max, resolution, resolution)
        if not gdf.crs:
            raise RuntimeError('GeoDataFrame has no CRS (creating GeoTIFF)')
        
        out_meta = {
            'driver': 'GTiff',
            'height': y_res,
            'width': x_res,
            'count': 2,
            'dtype': 'float32',
            'crs': gdf.crs.to_string(),
            'transform': transform,
            'nodata': 1000000,
            'compress': 'lzw',
            'interleave': 'band'
        }

        with rasterio.open(filename, "w", **out_meta) as dest:
            for idx, col in enumerate(['depth', 'uncertainty'], start=1):
                array = np.full((y_res, x_res), out_meta['nodata'], dtype='float32')
                for point, value in zip(gdf.geometry, gdf[col]):
                    assert isinstance(point, Point) # Pylance checks for correct geometry!
                    col_idx = int((point.x - x_min) / resolution)
                    row_idx = int((y_max - point.y) / resolution)
                    if 0 <= col_idx < x_res and 0 <= row_idx < y_res:
                        array[row_idx, col_idx] = value
                dest.write(array, idx)

    except Exception as e:
        raise RuntimeError(f'Failed to create GeoTIFF for {filename.as_posix()}; Error: {format_exc()}')

def gpkgs_to_geotiffs(source_dir: Path, dest_dir: Path, resolution: float = 8.0, **kwargs) -> None:
    '''Transform a directory of GeoPackage files into a corresponding set of GeoTIFFs

    This reads all GeoPackage (.gpkg) files in the source directory into GeoTIFFs, storing the
    results with equivalent names in the destination directory.  By default, only the non-outlier
    points in the GeoPackages are used to construct the GeoTIFF.

    :param: source_dir  Path object for directory with GeoPackages
    :param: dest_dir    Path object for directory to store GeoTIFFs
    :param: resolution  Float for resolution at which to create GeoTIFFs
    '''
    verbose: bool = False
    if 'verbose' in kwargs:
        verbose = kwargs['verbose']

    if dest_dir.exists() and not dest_dir.is_dir():
        raise ValueError('nominal destination directory is not actually a directory')
    dest_dir.mkdir(parents=True, exist_ok=True)

    for filename in source_dir.glob('Processed_*.gpkg'):
        if verbose:
            print(f"[blue]Debug:[/] Generating GeoTIFF for {filename}...")

        try:
            gdf = gpd.read_file(filename)
            cleaned_gdf = gdf[gdf['Outlier'] == False]
            geotiff_name: Path = filename.with_suffix('.tif')
            create_geotiff(cleaned_gdf, geotiff_name, resolution=resolution)
            if verbose:
                print(f"[blue]Debug:[/] Saved GeoTIFF to {geotiff_name}")

        except Exception as e:
            print(f"[red]Error:[/] Failed generating GeoTIFF for {filename}: {e}")
