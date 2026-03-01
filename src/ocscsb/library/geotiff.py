from typing import cast, Any
from pathlib import Path
import numpy as np
import geopandas as gpd
from shapely.geometry import Point, Polygon
import pyproj
import rasterio
from rasterio.transform import from_origin, from_bounds
from rasterio.features import rasterize
from rasterio.warp import transform_bounds
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
        raise RuntimeError(f'Failed to create GeoTIFF for {filename}; Error: {format_exc()}')

def rasterize_geotiff(gdf: gpd.GeoDataFrame, filename: Path, resolution: float = 8.0) -> None:
    output_bounds = gdf.total_bounds
    ncols = int((output_bounds[2] - output_bounds[0]) / resolution)
    nrows = int((output_bounds[3] - output_bounds[1]) / resolution)

    # Create the affine transform from bounds
    transform = from_bounds(
        output_bounds[0], output_bounds[1], output_bounds[2], output_bounds[3],
        ncols,
        nrows
    )
    filename.parent.mkdir(parents=True, exist_ok=True)

    # Create an empty raster with the desired nodata value and burn the depth_mod values.
    with rasterio.open(
        filename,
        "w",
        driver="GTiff",
        height=nrows,
        width=ncols,
        compress='lzw',
        count=1,  # Single band output
        dtype="float32",
        crs=gdf.crs,
        transform=transform,
        nodata=1000000,  # Set the nodata value here
    ) as dst:
        # Burn the vector data into the raster.
        # For each geometry, use its corresponding depth_mod value.
        burned = rasterize(
            ((geom, value) for geom, value in zip(gdf.geometry, gdf['depth_mod'])),
            out_shape=dst.shape,
            fill=1000000,  # Background value (nodata)
            transform=dst.transform,
            dtype="float32"
        )
        dst.write(burned, 1)

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

def get_bbox_wgs84(filename: Path, **kwargs) -> dict[str,Any]:
    '''
    Return the boiunding box from a given GeoTIFF file (typically a BlueTopo tile in this
    context), converting into WGS84 so that it's comparable to the depth observations.

    :param: Path to the GeoTIFF to load
    :return: Dictionary for the bounds (float) and the CRS for the GeoTIFF
    '''
    verbose = kwargs.get('verbose', False)
    with rasterio.open(filename) as src:
        src_crs = src.crs
        src_bounds = src.bounds  # (left, bottom, right, top)
        if verbose:
            print(f"[blue]Debug:[/] BlueTopo tile native CRS: {src_crs}")
            print(f"[blue]Debug:[/] Original bounds (native CRS): {src_bounds}")
        
        # Transform bounds to WGS84 (EPSG:4326)
        dst_crs = "EPSG:4326"
        bounds_wgs84 = transform_bounds(
            src_crs, dst_crs,
            src_bounds.left, src_bounds.bottom,
            src_bounds.right, src_bounds.top,
            densify_pts=21
        )
        if verbose:
            print(f"[blue]Debug:[/] Transformed bounds in WGS84: {bounds_wgs84}")
        
        return {
            "min_lon": bounds_wgs84[0],
            "min_lat": bounds_wgs84[1],
            "max_lon": bounds_wgs84[2],
            "max_lat": bounds_wgs84[3],
            "src_crs": src_crs
        }

def sample_grid(filename: Path, gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    """
    Reprojects the GeoDataFrame from WGS84 to the target CRS (the BlueTopo tile's CRS),
    then uses Rasterio's vectorized sampling to retrieve the BlueTopo value for each point.
    The sampled value is added as a new column 'bluetopo_value' and discrepancy is computed.
    """
    with rasterio.open(filename) as src:
        gdf_proj = gdf.to_crs(src.crs)
        coords = [(cast(Point, geom).x, cast(Point, geom).y) for geom in gdf_proj.geometry]
        sampled_values = [val[0] for val in src.sample(coords)]
    gdf['bluetopo_value'] = sampled_values
    gdf['discrepancy'] = gdf['depth_mod'] - gdf['bluetopo_value']
    return gdf

def diff_grid_to_geotiff(grid: gpd.GeoDataFrame, filename: Path, nodata: int=1000000, **kwargs) -> None:
    """
    Exports the aggregated difference grid (with a 'mean_diff' column) to a GeoTIFF.
    Grid cells with no data are assigned the specified nodata value.
    """
    verbose = kwargs.get('verbose', False)
    bounds = grid.total_bounds  # [xmin, ymin, xmax, ymax]
    xmin, ymin, xmax, ymax = bounds
    # Use the width of the first grid cell to determine resolution.
    sample_bounds = cast(Polygon, grid.geometry.iloc[0]).bounds
    cell_width = sample_bounds[2] - sample_bounds[0]
    resolution = cell_width
    ncols = int((xmax - xmin) / resolution)
    nrows = int((ymax - ymin) / resolution)
    transform = from_bounds(xmin, ymin, xmax, ymax, ncols, nrows)
    
    # Use the 'mean_diff' value for each grid cell; fill missing cells with nodata.
    shapes_gen = ((geom, value) for geom, value in zip(grid.geometry, grid['mean_diff'].fillna(nodata)))
    raster_array = rasterize(
        shapes_gen,
        out_shape=(nrows, ncols),
        transform=transform,
        fill=nodata,
        dtype='float32'
    )
    assert isinstance(grid.crs, pyproj.CRS)
    out_meta = {
        'driver': 'GTiff',
        'height': nrows,
        'width': ncols,
        'count': 1,
        'dtype': 'float32',
        'crs': grid.crs.to_string(),
        'transform': transform,
        'nodata': nodata
    }
    filename.parent.mkdir(parents=True, exist_ok=True)
    with rasterio.open(filename, "w", **out_meta) as dst:
        dst.write(raster_array, 1)
    if verbose:
        print(f"[blue]Debug:[/] Difference grid GeoTIFF created at {filename}")
