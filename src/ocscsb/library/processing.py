import os
import tempfile
from datetime import datetime, timedelta
import shutil
import time
from importlib import resources
from pathlib import Path
from typing import Callable, Any
import traceback as tb
import gc
from io import BytesIO
import contextlib

from osgeo import gdal
gdal.UseExceptions()

import requests
import geopandas as gpd
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import rasterio
from osgeo import gdal
from rasterio.features import shapes
from rasterio.transform import from_origin
from rasterio.warp import Resampling
from shapely.geometry import shape, Point
from shapely.validation import make_valid
import duckdb

from shapely.ops import unary_union
from skimage.morphology import dilation, erosion
from scipy.interpolate import interp1d
import seaborn as sns
from sklearn.experimental import enable_iterative_imputer
from sklearn.impute import IterativeImputer
from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import StandardScaler
from scipy.ndimage import uniform_filter1d

from ocscsb.library.fes_model import get_fes_tide, get_lat_separation
from ocscsb.library import bluetopo, io

GDAL_VSI_PREFIX: str = '/vsis3/'
S3_PATH_SEP: str = '/'
FINAL_PROD_DIR = 'final_products'

# def setup_logging(output_dir):
#     """Configures logging to print to both console and a file."""
#     log_file = os.path.join(output_dir, f"processing_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.txt")
#
#     # Configure the logger
#     logging.basicConfig(
#         level=logging.INFO,
#         format='%(asctime)s - %(levelname)s - %(message)s',
#         # Specify UTF-8 encoding for the file handler to support all characters
#         handlers=[
#             logging.FileHandler(log_file, encoding='utf-8'),
#             logging.StreamHandler(sys.stdout)
#         ]
#     )
#
#     # Redirect the built-in print function to the logger
#     def logger_print(*args, **kwargs):
#         logging.info(' '.join(map(str, args)))
#
#     if isinstance(__builtins__, dict):
#         __builtins__['print'] = logger_print
#     else:
#         __builtins__.print = logger_print
#
#     print("--- Logging configured. Output will be saved to log file. ---")


def get_utm_zone_wgs84(lat, lon):
    """
    Calculates the WGS84 UTM zone EPSG code for a given latitude and longitude.
    This function correctly handles both Northern and Southern hemispheres.
    """
    zone_number = int((lon + 180) // 6) + 1

    # Southern hemisphere EPSG codes are in the 327xx range
    if lat < 0:
        epsg_code = 32700 + zone_number
    # Northern hemisphere EPSG codes are in the 326xx range
    else:
        epsg_code = 32600 + zone_number

    return epsg_code

def get_utm_zone_nad83(lat, lon):
    """
    Calculates the NAD83 UTM zone EPSG code for a given latitude and longitude.
    """
    zone_number = int((lon + 180) // 6) + 1
    if lat < 0:
        raise ValueError("NAD83 UTM zones are generally for northern hemisphere data only.")
    return 26900 + zone_number


class ProcessingException(Exception):
    ...


class Processor:
    def __init__(self,
                 csb_directory: str,
                 output_dir: str,
                 *,
                 provider: str = io.STORAGE_PROVIDER_TYPE_DEFAULT,
                 provider_args: dict[str, Any] | None = None,
                 clean_up_callback: Callable | None = None,
                 bag_file_path: str | None = None,
                 fp_zones: str | None = None,
                 use_bluetopo: bool = True,
                 use_fes_model: bool = True,
                 fes_data_path: str | None = None,
                 fes_yaml_path: str | None = None,
                 run_analysis: bool = False,
                 export_transits: bool = False,
                 run_final_grid: bool = False,
                 export_gp: bool = False,
                 insert_duckdb: bool = False,
                 export_final_gpkg: bool = False,
                 tessellation_shp: str | None = None,
                 grid_resolution: float = 10.0,
                 organize_vrt: bool = False):
        self.tmp_dir: Path = Path(tempfile.mkdtemp())
        if provider_args:
            self.provider_args = provider_args
        else:
            self.provider_args = {}
        self.csb_directory: io.StorageLocation = io.StorageLocation(csb_directory, provider=provider,
                                                                    **self.provider_args)

        self.use_bluetopo = use_bluetopo
        self.bag_file_path: io.File | None = None
        if bag_file_path and bag_file_path != '':
            self.bag_file_path: io.File = io.File.init(bag_file_path, provider=provider,
                                                       **self.provider_args)

        self.output_dir: io.StorageLocation = io.StorageLocation(output_dir, provider=provider,
                                                                 **self.provider_args)
        self.final_products_loc: io.StorageLocation = self.output_dir.sub_location('final_products')

        if fp_zones is None or fp_zones == '':
            fp_zone_path: str = os.path.abspath(str(resources.files('ocscsb').joinpath('data/tide_zone_polygons.sqlite')))
        else:
            fp_zone_path: str = fp_zones
        self.fp_zones = io.File.init(fp_zone_path)

        self.use_fes_model = use_fes_model
        self.fes_data_path: Path | None = None
        if fes_data_path and fes_data_path != '':
            # PyFES needs to have its DATASET_DIR env variable be a local path set from a string, so we can't use
            # io.StorageLocation
            self.fes_data_path = Path(fes_data_path).absolute()
        self.fes_yaml_path: Path | None = None
        if fes_yaml_path and fes_yaml_path != '':
            # PyFES needs to read its YAML config from a local file, so we can't use io.StorageLocation
            self.fes_yaml_path = Path(fes_yaml_path).absolute()

        self.master_offset_file: io.File = self.output_dir.new_file('master_offsets.csv')
        self.master_offsets: pd.DataFrame = self.read_master_offsets()

        self.duckdb_path: Path = self.tmp_dir / 'csb.duckdb'

        self.run_analysis = run_analysis
        self.export_transits = export_transits
        self.run_final_grid = run_final_grid
        self.export_gp = export_gp
        self.insert_duckdb = insert_duckdb
        self.export_final_gpkg = export_final_gpkg
        self.tessellation_shp: io.File | None = None
        if tessellation_shp and tessellation_shp != '':
            pth = Path(tessellation_shp).absolute()
            self.tessellation_shp = io.File.from_path(pth)
        self.grid_resolution = grid_resolution
        self.organize_vrt = organize_vrt
        self.clean_up_callback = clean_up_callback

        # setup_logging(output_dir)
        print(f"output_dir is: {output_dir}")

    def load_csb(self, csb_file: io.File) -> gpd.GeoDataFrame:
        print('*****Reading CSB input csv file in chunks*****')

        chunk_size = 5_000_000
        processed_chunks = []

        # This creates an iterator that yields a DataFrame chunk on each loop
        with pd.read_csv(csb_file.open(), chunksize=chunk_size, low_memory=False) as reader:
            for i, chunk in enumerate(reader):
                print(f"Processing chunk {i + 1}...")

                # Apply the same cleaning and filtering logic to each chunk
                chunk['depth'] = pd.to_numeric(chunk['depth'], errors='coerce')
                chunk['time'] = pd.to_datetime(chunk['time'], errors='coerce')

                chunk = chunk.dropna(subset=['time', 'depth'])

                chunk = chunk[(chunk['depth'] > 0.5) & (chunk['depth'] < 1000)]

                lower_bound = pd.to_datetime("2014")
                upper_bound = pd.to_datetime(str(datetime.now().year + 1))
                chunk = chunk[(chunk['time'] > lower_bound) & (chunk['time'] < upper_bound)]

                chunk = chunk.drop_duplicates(subset=['lon', 'lat', 'depth', 'time', 'unique_id'])

                if not chunk.empty:
                    processed_chunks.append(chunk)

        if not processed_chunks:
            print("Warning: No valid data found in the CSV after filtering.")
            # Return an empty GeoDataFrame with expected columns to prevent downstream errors
            return gpd.GeoDataFrame([], columns=['lon', 'lat', 'depth', 'time', 'unique_id', 'geometry'], crs="EPSG:4326")

        print("Concatenating processed chunks...")
        # Combine all the processed chunks into a single DataFrame
        df = pd.concat(processed_chunks, ignore_index=True)

        # Create a GeoDataFrame using lon/lat columns
        gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.lon, df.lat), crs="EPSG:4326")
        return gdf

    def create_convex_hull_and_download_tiles(self,
                                              title: str,
                                              csb_file: io.File) -> io.File:
        """
        Loads CSB data, builds a convex hull, and writes it to a shapefile.
        As part of doing so, creates a dedicated Modeling folder,
        downloads BlueTopo tiles, copies them to a separate archive folder,
        and then builds a VRT from all GeoTIFF files found recursively in that folder.
        """

        # Load CSB data and create convex hull
        csb_data = pd.read_csv(csb_file.open())
        gdf = gpd.GeoDataFrame(csb_data, geometry=gpd.points_from_xy(csb_data.lon, csb_data.lat))
        gdf = gdf.set_crs(4326, allow_override=True)
        convex_hull_polygon = gdf.unary_union.convex_hull
        convex_hull_gdf = gpd.GeoDataFrame(geometry=[convex_hull_polygon], crs=gdf.crs)
        convex_hull_shapefile = self.tmp_dir / 'convex_hull_polygon.shp'
        convex_hull_gdf.to_file(convex_hull_shapefile)
        convex_hull_shapefile_ = str(convex_hull_shapefile)
        print(f"Convex hull shapefile written to: {convex_hull_shapefile_}")

        # Create the 'Modeling' folder for storing the VRT referencing GeoTIFFs in S3
        bluetopo_tiles_dir = self.tmp_dir / 'Modeling'
        bluetopo_tiles_dir_ = str(bluetopo_tiles_dir)
        print(f"Using bluetopo_tiles_dir: {bluetopo_tiles_dir_}...")
        bluetopo_tiles_dir.mkdir(exist_ok=True)

        # Identify BlueTopo tiles that correspond to this convex hull
        _, _, _, _, tiles = bluetopo.identify_tiles(bluetopo_tiles_dir_, convex_hull_shapefile_)
        tile_files = []
        for tile in tiles:
            tile_files.append(f"{GDAL_VSI_PREFIX}{tile.bucket}{S3_PATH_SEP}{tile.object}")
        # Build a VRT from tiles stored in S3, without needing to download them.
        vrt_dir = self.tmp_dir / f"BlueTopo_VRT_{title}"
        vrt_dir.mkdir(exist_ok=True)
        vrt_path = vrt_dir / f"merged_tiles_{title}.vrt"
        with gdal.config_option('AWS_NO_SIGN_REQUEST', 'YES'):
            gdal.BuildVRT(vrt_path, tile_files)
        print(f"Created VRT at {str(vrt_path)}")

        return io.File.from_path(vrt_path)

    def read_master_offsets(self) -> pd.DataFrame:
        """Reads the master offsets from a CSV file stored at `self.master_offset_file`."""
        if self.master_offset_file.exists():
            print(f"Found existing master offsets file at: {self.master_offset_file.get_uri()}")
            return pd.read_csv(self.master_offset_file.open())
        else:
            print("No master_offsets.csv found. Will create a new one.")
            return pd.DataFrame(
                columns=['unique_id', 'platform_name', 'offset_value', 'std_dev', 'accuracy_score', 'date_range',
                         'tile_name'])

    def update_master_offsets(self,
                              title: str,
                              unique_id: str,
                              platform_name: str,
                              new_offset: float,
                              std_dev: float,
                              date_range: tuple[str, str]):
        accuracy_score = 1 / std_dev if std_dev != 0 else 0

        #print('checking for existing offset by unique_id and platform_name')
        existing_index = self.master_offsets[(self.master_offsets['unique_id'] == unique_id)].index

        new_row = pd.DataFrame([{
            'unique_id': unique_id,
            'platform_name': platform_name,
            'offset_value': new_offset,
            'std_dev': std_dev,
            'accuracy_score': accuracy_score,
            'date_range': date_range,
            'tile_name': title
        }])

        # Exclude empty or all-NA entries before concatenation
        new_row = new_row.dropna(how='all')

        if existing_index.empty:
            self.master_offsets = pd.concat([self.master_offsets, new_row], ignore_index=True)
        else:
            if self.master_offsets.loc[existing_index[0], 'accuracy_score'] <= accuracy_score:
                self.master_offsets.loc[existing_index[0], list(new_row.columns)] = new_row.iloc[0]

        try:
            self.master_offsets.to_csv(self.master_offset_file.open(mode='wb'), index=False)
        except Exception as e:
            print(f"Failed to update master offsets: {e}")

    def fetch_tide_data(self,
                        station_id, start_date, end_date, product, interval=None, attempt_great_lakes=False):
        base_url = "https://api.tidesandcurrents.noaa.gov/api/prod/datagetter"
        params = {
            "begin_date": start_date,
            "end_date": end_date,
            "station": station_id,
            "datum": "MLLW" if not attempt_great_lakes else "LWD",
            "time_zone": "gmt",
            "units": "metric",
            "format": "json",
            "product": product
        }

        if interval:
            params["interval"] = interval

        request_url = requests.Request('GET', base_url, params=params).prepare().url
        print(f"Requesting URL: {request_url}")

        response = requests.get(base_url, params=params)
        data = response.json()

        if 'predictions' in data:
            df = pd.json_normalize(data['predictions'])
            data_type = "predicted data"
        elif 'data' in data:
            df = pd.json_normalize(data['data'])
            data_type = "observed data"
        else:
            print(f"No data returned for URL: {request_url}")
            return pd.DataFrame()

        df['t'] = pd.to_datetime(df['t'])
        # Convert 'v' to numeric, coercing errors to NaN
        df['v'] = pd.to_numeric(df['v'], errors='coerce')
        if df['v'].isna().any():
            print("Warning: Some tide values could not be converted to numeric and will be dropped.")
            df = df.dropna(subset=['v'])

        print(f"Pulled {data_type} for station {station_id} from {start_date} to {end_date}")
        return df

    def check_for_gaps(self, df, max_gap_duration='1h'):
        gaps = df['t'].diff() > pd.Timedelta(max_gap_duration)
        return gaps.any()

    def cosine_interpolation(self, df, start_date, end_date):
        df['time_num'] = (df['t'] - pd.Timestamp("1970-01-01")) // pd.Timedelta('1s')
        df = df.sort_values('time_num')
        interp_func = interp1d(df['time_num'], df['v'], kind='cubic')
        time_num_grid = np.linspace(df['time_num'].min(), df['time_num'].max(), num=3000)
        v_grid = interp_func(time_num_grid)
        time_grid = pd.to_datetime(time_num_grid, unit='s')

        # Trimming
        trim_start = pd.to_datetime(start_date) + pd.Timedelta(hours=12)
        trim_end = pd.to_datetime(end_date) - pd.Timedelta(hours=12)
        trimmed_df = pd.DataFrame({'t': time_grid, 'v': v_grid})
        trimmed_df = trimmed_df[(trimmed_df['t'] >= trim_start) & (trimmed_df['t'] <= trim_end)]
        return trimmed_df

    def create_survey_outline(self,
                              title: str,
                              raster_path: Path, *,
                              desired_resolution: float = 8,
                              dilation_iterations: int = 3,
                              erosion_iterations: int = 2) -> Path:
        print("starting create_survey_outline() function")
        with rasterio.open(raster_path) as raster:
            # Resample the raster
            data = raster.read(
                1,  # Reading only the first band
                out_shape=(
                    raster.height // desired_resolution,
                    raster.width // desired_resolution
                ),
                resampling=Resampling.bilinear
            )

            # Create a binary mask
            nodata = raster.nodatavals[0] or 1000000
            binary_mask = (data != nodata).astype(np.uint8)

            # Apply dilation and erosion
            for _ in range(dilation_iterations):
                binary_mask = dilation(binary_mask)
            for _ in range(erosion_iterations):
                binary_mask = erosion(binary_mask)

            # Ensure binary_mask is of type uint8
            binary_mask = binary_mask.astype(np.uint8)

            # Generate shapes from the binary mask
            transform = raster.transform * raster.transform.scale(
                (raster.width / data.shape[-1]),
                (raster.height / data.shape[-2])
            )

            # Generate polygons from the binary mask and make them valid
            polygons = [make_valid(shape(geom)) for geom, val in shapes(binary_mask, mask=binary_mask, transform=transform) if val == 1]

            # Simplify polygons to reduce complexity
            simplified_polygons = [polygon.simplify(tolerance=0.001, preserve_topology=True) for polygon in polygons]

            # Perform unary union on simplified, valid polygons
            unified_geometry = unary_union(simplified_polygons)
            # Reproject unified geometry to WGS84 before simplification
            geo_df = gpd.GeoDataFrame(geometry=[unified_geometry], crs=raster.crs)
            geo_df = geo_df.to_crs(epsg=4326)

            # Simplify the geometry
            geo_df['geometry'] = geo_df.geometry.simplify(tolerance=0.001)

            # Check and fix bad topology if necessary
            geo_df['geometry'] = geo_df.geometry.apply(lambda geom: geom.buffer(0) if not geom.is_valid else geom)

            # Save to a shapefile
            bathy_polygon_shp = self.tmp_dir / f"{title}_bathy_polygon.shp"
            geo_df.to_file(bathy_polygon_shp, driver='ESRI Shapefile')

            print(f"Bathymetry polygon shapefile {str(bathy_polygon_shp)} created.")
            return bathy_polygon_shp

    def apply_fes_tides(self, gdf):
        """
        Applies tides using the FES global model and transforms the result
        directly to be referenced to Lowest Astronomical Tide (LAT).
        """
        print("***** Applying tides using global FES model (referenced to LAT) *****")

        if self.fes_data_path is None:
            raise ValueError("FES Model requires both a data path and a YAML config file.")

        lons = gdf['lon'].to_numpy()
        lats = gdf['lat'].to_numpy()
        times = gdf['time'].to_numpy(dtype='datetime64[us]')

        # Step 1: Get the standard tide prediction relative to MSL
        msl_tide_values = get_fes_tide(lons, lats, times, self.fes_data_path, self.fes_yaml_path)

        # Step 2: Get the MSL-to-LAT separation value for each point using the fast formula
        lat_separation_values = get_lat_separation(lons, lats, self.fes_data_path)

        # Step 3: Combine them to get the final tide corrector relative to LAT
        # CORRECT FORMULA: Tide relative to LAT = Tide relative to MSL + Separation from MSL down to LAT
        lat_tide_values = msl_tide_values + lat_separation_values

        # Apply the final LAT-referenced correction to the raw depths
        gdf['depth_new'] = gdf['depth'] - lat_tide_values

        # Clean up and prepare for next steps
        gdf = gdf.rename(columns={'depth': 'depth_old'})

        print("***** FES tide correction complete. Depths are referenced to approx. LAT. *****")
        return gdf

    def tides(self, csb_file: io.File) -> pd.DataFrame:
        gdf: gpd.GeoDataFrame = self.load_csb(csb_file)

        if self.use_fes_model:
            # --- Route to the new FES model function ---
            # This path completely bypasses the NOAA zoned tide logic
            csb_corr = self.apply_fes_tides(gdf)

            # Add columns that are expected by later functions but not created by FES
            csb_corr['ControlStn'] = 'FES_Model'
            csb_corr['time'] = csb_corr['time'].dt.strftime("%Y%m%d %H:%M:%S")
            csb_corr = gpd.GeoDataFrame(csb_corr, geometry='geometry', crs='EPSG:4326')

        else:
            # --- Route to the original NOAA zoned tide logic ---
            print('CSB data from csv file loaded. Starting NOAA tide correction')

            zones = gpd.read_file(self.fp_zones.open())
            join = gpd.sjoin(gdf, zones, how='inner', predicate='within')
            join = join.astype({'time': 'datetime64[us]'})
            join = join.sort_values('time')

            def generate_date_ranges(dates):
                dates.sort()
                date_ranges = []
                for date in dates:
                    if not date_ranges or date - pd.Timedelta(days=1) > date_ranges[-1][1]:
                        date_ranges.append([date, date])
                    else:
                        date_ranges[-1][1] = date
                date_ranges = [(start_date - pd.Timedelta(days=1), end_date + pd.Timedelta(days=1)) for start_date, end_date
                               in date_ranges]
                return [(start_date.strftime('%Y%m%d'), end_date.strftime('%Y%m%d')) for start_date, end_date in
                        date_ranges]

            tdf = []
            known_subordinate_stations = set()
            known_great_lakes_stations = set()

            for station_id in join['ControlStn'].unique():
                station_dates = join[join['ControlStn'] == station_id]['time'].dt.floor('d').unique()
                date_ranges = generate_date_ranges(list(station_dates))

                for start_date, end_date in date_ranges:
                    # Try to fetch observed data first
                    verified_data = self.fetch_tide_data(station_id, start_date, end_date, product="water_level")
                    if not verified_data.empty and not self.check_for_gaps(verified_data):
                        tdf.append(verified_data)
                    else:
                        # If there are gaps, try fetching 6-minute predicted data
                        predicted_data = self.fetch_tide_data(station_id, start_date, end_date, product="predictions")
                        if not predicted_data.empty and not self.check_for_gaps(predicted_data):
                            tdf.append(predicted_data)
                        else:
                            # If that doesn't work, fallback to predicted hilo data
                            hilo_predictions = self.fetch_tide_data(station_id, start_date, end_date, product="predictions",
                                                                    interval='hilo')
                            if not hilo_predictions.empty:
                                interpolated_hilo = self.cosine_interpolation(hilo_predictions, start_date, end_date)
                                tdf.append(interpolated_hilo)
                                known_subordinate_stations.add(station_id)
                            else:
                                great_lakes_data = self.fetch_tide_data(station_id, start_date, end_date, product="water_level",
                                                                        attempt_great_lakes=True)
                                if not great_lakes_data.empty:
                                    tdf.append(great_lakes_data)
                                    known_great_lakes_stations.add(station_id)
                                else:
                                    print(f"No water level data available for station {station_id}.")
            if tdf:
                tdf = pd.concat(tdf)
                print("Concatenated tdf shape:", tdf.shape)

                tdf = tdf.sort_values('t')
                jtdf = pd.merge_asof(join, tdf, left_on='time', right_on='t')
                print("jtdf shape before column drop:", jtdf.shape)

                columns_to_drop = ['Shape__Are', 'Shape__Len', 'Input_FID', 'id', 'name', 'state', 'affil',
                                   'latitude', 'longitude', 'data', 'metaapi', 'dataapi', 'Shape_Le_2']
                jtdf.drop(columns=columns_to_drop, inplace=True, errors='ignore')
                print("jtdf shape after column drop:", jtdf.shape)

                jtdf = jtdf.dropna(subset=['depth', 'time', 'geometry'])
                print("jtdf shape after dropna:", jtdf.shape)

                jtdf['t_corr'] = jtdf['t'] + pd.to_timedelta(jtdf['ATCorr'], unit='m')

                newdf = jtdf[['t_corr', 'v']].copy()
                print("newdf shape before dropna:", newdf.shape)

                newdf = newdf.rename(columns={'v': 'v_new', 't_corr': 't_new'})
                newdf = newdf.sort_values('t_new').dropna()
                print("newdf shape after dropna:", newdf.shape)

                csb_corr = pd.merge_asof(jtdf, newdf, left_on='time', right_on='t_new', direction='nearest')
                print("csb_corr shape before dropna:", csb_corr.shape)

                print("csb_corr shape after dropna:", csb_corr.shape)

                csb_corr['depth_new'] = csb_corr['depth'] - (csb_corr['RR'] * csb_corr['v_new'])
                print("csb_corr shape after applying tide corrections:", csb_corr.shape)

                csb_corr = gpd.GeoDataFrame(csb_corr, geometry='geometry', crs='EPSG:4326')
                csb_corr['time'] = csb_corr['time'].dt.strftime("%Y%m%d %H:%M:%S")

                csb_corr = csb_corr[(csb_corr['depth'] > 1.5) & (csb_corr['depth'] < 1000)]
                csb_corr = csb_corr.rename(columns={'depth': 'depth_old'}).drop(
                    columns=['index_right', 'ATCorr', 'RR', 'ATCorr2', 'RR2', 'Shape_Leng', 'Shape_Area', 'Shape_Le_1', 't',
                             'v', 't_corr', 't_new', 'v_new'])
            else:
                print("No tide data available for the specified period.")
                # Return an empty DataFrame but with columns expected by later steps to avoid errors
                csb_corr = pd.DataFrame(columns=gdf.columns.tolist() + ['depth_old', 'depth_new'])

        return csb_corr

    def extract_bag(self, title: str, bag_file: io.File) -> tuple[Path, Path]:
        print("starting BAGextract() function")
        print("DEBUG - bag_file: ", bag_file.get_uri())
        print('*****Starting to import reference bathy*****')

        output_raster_wgs84 = self.tmp_dir / f"{title}_wgs84.tif"

        print("Warping input raster to standard WGS84 (EPSG:4269)...")

        input_for_warp = bag_file.get_gdal_vsi_path()

        # For BAG files, we first create a VRT to select the depth and uncertainty bands
        # if BAG_filepath.lower().endswith('.bag'):
        if bag_file.get_suffix() == '.bag':
            print("BAG file detected, creating temporary VRT to select bands 1 and 2...")
            # gdal.BuildVRT is the correct place to use bandList
            temp_vrt_path: Path = self.tmp_dir / 'temp_for_warp.vrt'
            gdal.BuildVRT(temp_vrt_path, bag_file.get_gdal_vsi_path(), bandList=[1, 2])
            input_for_warp = temp_vrt_path

        # BIGTIFF=YES' to creationOptions to allow files larger than 4GB
        if self.use_bluetopo:
            with gdal.config_option('AWS_NO_SIGN_REQUEST', 'YES'):
                gdal.Warp(output_raster_wgs84, input_for_warp,
                          dstSRS='EPSG:4326',
                          creationOptions=['COMPRESS=LZW', 'BIGTIFF=YES'],
                          dstNodata=1000000)
        else:
            gdal.Warp(output_raster_wgs84, input_for_warp,
                      dstSRS='EPSG:4326',
                      creationOptions=['COMPRESS=LZW', 'BIGTIFF=YES'],
                      dstNodata=1000000)

        print("Reference raster prepared successfully.")

        # Call create_survey_outline to generate the bathymetry polygon shapefile
        bathy_polygon_shp: Path = self.create_survey_outline(title, output_raster_wgs84)

        return output_raster_wgs84, bathy_polygon_shp

    def get_raster_values_vectorized(self, coords, raster_path, batch_size=100000):
        """
        Given a list of (x, y) coordinate pairs and a raster file path,
        returns a list of lists, each containing the pixel values from
        all bands at that coordinate.

        If the number of coordinates exceeds batch_size, processing is done in batches.
        Pixels with nodata values are replaced with np.nan.
        """
        all_samples = []

        with rasterio.open(raster_path) as src:
            nodata = src.nodatavals  # Tuple of nodata values for each band

            # Process in batches if necessary to manage memory usage
            if len(coords) > batch_size:
                for i in range(0, len(coords), batch_size):
                    batch_coords = coords[i:i + batch_size]
                    batch_samples = list(src.sample(batch_coords))
                    all_samples.extend(batch_samples)
            else:
                all_samples = list(src.sample(coords))

        # Process the samples to replace nodata values with np.nan
        processed_samples = []
        for sample in all_samples:
            processed = []
            for i, val in enumerate(sample):
                if nodata[i] is not None and val == nodata[i]:
                    processed.append(np.nan)
                else:
                    processed.append(val)
            processed_samples.append(processed)

        return processed_samples

    def derive_draft(self,
                     title: str,
                     csb_file: io.File,
                     bag_file: io.File, *,
                     report=None) -> pd.DataFrame:
        output_raster, raster_boundary_shp = self.extract_bag(title, bag_file)
        csb_corr: pd.DataFrame = self.tides(csb_file)

        vessels_with_offsets = self.master_offsets['unique_id'].unique().tolist()
        if report:
            report.add_statistic("Vessels with existing offsets", len(vessels_with_offsets))

        raster_boundary = gpd.read_file(raster_boundary_shp)
        raster_boundary['geometry'] = raster_boundary['geometry'].apply(
            lambda geom: geom if geom.is_valid else geom.buffer(0)
        )
        csb_corr['geometry'] = csb_corr['geometry'].apply(
            lambda geom: geom if geom.is_valid else geom.buffer(0)
        )
        csb_corr['row_id'] = range(len(csb_corr))

        boundary_union = raster_boundary.geometry.unary_union

        possible_matches_index = csb_corr.sindex.query(boundary_union, predicate="intersects")
        possible_matches = csb_corr.iloc[possible_matches_index]

        csb_corr_subset = possible_matches[possible_matches.geometry.within(boundary_union)]

        csb_for_offset_derivation = csb_corr_subset[~csb_corr_subset['unique_id'].isin(vessels_with_offsets)]

        if not csb_for_offset_derivation.empty:
            num_new_vessels = csb_for_offset_derivation['unique_id'].nunique()
            if report: report.add_statistic("New vessels requiring offset calculation", num_new_vessels)

        sampled_csb_corr_subset = csb_corr_subset.copy()

        date_ranges = {}
        for name, group in csb_corr_subset.groupby('unique_id'):
            group['time'] = pd.to_datetime(group['time'])
            min_timestamp = group['time'].min()
            max_timestamp = group['time'].max()
            if pd.notnull(min_timestamp) and pd.notnull(max_timestamp):
                date_ranges[name] = (min_timestamp.strftime('%Y%m%d'), max_timestamp.strftime('%Y%m%d'))
            else:
                date_ranges[name] = ("19700101", "19700101")

        try:
            coords = [(geom.x, geom.y) for geom in sampled_csb_corr_subset.geometry]
            raster_samples = self.get_raster_values_vectorized(coords, output_raster)
            sampled_csb_corr_subset['Raster_Value'] = [vals[0] for vals in raster_samples]
            # This part correctly assigns NaN if only one band exists
            sampled_csb_corr_subset['Uncertainty_Value'] = [
                vals[1] if len(vals) > 1 else np.nan for vals in raster_samples
            ]
        except Exception as e:
            print(f"Unexpected error encountered during selection of Raster_Value from reference bathy: {e}")

        csb_corr = csb_corr.merge(
            sampled_csb_corr_subset[['row_id', 'Raster_Value', 'Uncertainty_Value']],
            on='row_id', how='left'
        )

        # Keep rows if Uncertainty < 4 OR if Uncertainty is missing (NaN).
        filtered_csb_corr = csb_corr[(csb_corr['Uncertainty_Value'] < 4) | (csb_corr['Uncertainty_Value'].isna())]

        # We only want to derive offsets for new vessels
        filtered_csb_corr = filtered_csb_corr[
            filtered_csb_corr['unique_id'].isin(csb_for_offset_derivation['unique_id'].unique())]

        try:
            if 'Raster_Value' in filtered_csb_corr.columns and 'depth_new' in filtered_csb_corr.columns:
                filtered_csb_corr['diff'] = filtered_csb_corr['depth_new'] - (filtered_csb_corr['Raster_Value'] * -1)
        except Exception as e:
            print(f"Unexpected error encountered calculating diff: {e}")

        if not filtered_csb_corr.empty and 'diff' in filtered_csb_corr.columns:
            try:
                out = filtered_csb_corr.groupby('unique_id')['diff'].agg(['mean', 'std', 'count']).reset_index()

                out.loc[:, 'mean'] = out['mean'].fillna(0)
                out.loc[:, 'std'] = out['std'].fillna(999)
                out.loc[:, 'count'] = out['count'].fillna(0)
                out.loc[(out['mean'] > 3) | (out['mean'] < -11), ['mean', 'std', 'count']] = [0, 999, 0]
                out.loc[(out['std'] > 7), ['mean', 'std', 'count']] = [0, 999, 0]
                vessel_offsets_file: io.File = self.output_dir.new_file(f"VESSEL_OFFSETS_csb_corr_{title}.csv")
                out.to_csv(vessel_offsets_file.open(mode='ab'))

                platform_mapping = filtered_csb_corr[['unique_id', 'platform_name']].drop_duplicates()
                out_with_platform = out.merge(platform_mapping, on='unique_id', how='left')

                if report and not out_with_platform.empty:
                    report.add_statistic("New offsets successfully calculated", len(out_with_platform))

                for index, row in out_with_platform.iterrows():
                    unique_id = row['unique_id']
                    platform_name = row['platform_name']
                    new_offset = row['mean']
                    std_dev = row['std']
                    date_range = date_ranges.get(unique_id, ("19700101", "19700101"))
                    self.update_master_offsets(title, unique_id, platform_name, new_offset, std_dev, date_range)
            except Exception as e:
                print(f"Unexpected error encountered creating aggregation dataframe: {e}")

        return csb_corr

    def insert_into_duckdb(self, gdf: gpd.GeoDataFrame):
        """
        Inserts (or appends) data, automatically upgrading the table schema if necessary.
        -- FINAL, ROBUST VERSION --
        """
        df = gdf.copy()
        if 'geometry' in df.columns:
            df['wkb_geom'] = df['geometry'].apply(lambda geom: geom.wkb if geom is not None else None)
            df = df.drop(columns='geometry')
        else:
            df['wkb_geom'] = None

        # This master list defines the complete, final schema.
        master_columns_with_types = {
            "controlstn": "VARCHAR", "Raster_Value": "DOUBLE", "Uncertainty_Value": "DOUBLE",
            "accuracy_score": "DOUBLE", "date_range": "VARCHAR", "depth_new": "DOUBLE",
            "depth_old": "DOUBLE", "depthfinal": "DOUBLE", "lat": "FLOAT", "lon": "FLOAT",
            "offset_value": "DOUBLE", "platform_name_x": "VARCHAR", "provider": "VARCHAR",
            "std_dev": "DOUBLE", "tile_name": "VARCHAR", "time": "VARCHAR", "unique_id": "VARCHAR",
            "wkb_geom": "BLOB", "diff": "DOUBLE", "depth_mod": "DOUBLE", "uncertainty_vert": "DOUBLE",
            "uncertainty_hori": "DOUBLE", "Outlier": "BOOLEAN", "transit_id": "VARCHAR",
            "vessel_speed_smoothed": "DOUBLE"
        }
        master_columns = list(master_columns_with_types.keys())

        # Ensure the incoming DataFrame has all columns, filling missing with None
        for col in master_columns:
            if col not in df.columns:
                df[col] = None
        df = df[master_columns]  # Ensure consistent order

        try:
            with duckdb.connect(database=self.duckdb_path, read_only=False) as con:
                # Check if the 'csb' table exists
                table_exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='csb'").fetchone()

                if table_exists:
                    # --- SCHEMA MIGRATION LOGIC ---
                    # Table exists, so check and add any missing columns.
                    existing_columns_df = con.execute("DESCRIBE csb").fetchdf()
                    existing_columns = existing_columns_df['column_name'].tolist()

                    for col_name, col_type in master_columns_with_types.items():
                        if col_name not in existing_columns:
                            print(f"Schema mismatch detected. Adding missing column '{col_name}' to the 'csb' table.")
                            con.execute(f"ALTER TABLE csb ADD COLUMN {col_name} {col_type};")
                else:
                    # Table does not exist, create it with the full schema.
                    print('TABLE csb does not exist, creating it...')
                    columns_for_create = ", ".join(
                        [f"{name} {dtype}" for name, dtype in master_columns_with_types.items()])
                    con.execute(f"CREATE TABLE csb ({columns_for_create})")

                # Now, the table schema is guaranteed to match our DataFrame.
                # Use an explicit column list in the INSERT statement for maximum safety.
                col_list_for_insert = ", ".join(master_columns)
                con.register("temp_df", df)
                con.execute(f"INSERT INTO csb ({col_list_for_insert}) SELECT * FROM temp_df")

            print(f"Data successfully appended to DuckDB table at {str(self.duckdb_path)}.")
        except Exception as e:
            print(f"CRITICAL ERROR inserting into DuckDB: {e}")
            raise e

    def draft_corr(self,
                   title: str,
                   csb_file: io.File,
                   bag_file: io.File) -> gpd.GeoDataFrame:
        csb_corr: pd.DataFrame = self.derive_draft(title, csb_file, bag_file)

        # Merge the CSB data with the master offsets based on unique vessel ID
        # This will now include any newly derived offsets from the step above
        csb_corr1 = csb_corr.merge(self.master_offsets, on='unique_id', how='left')

        # Apply the offset correction
        # Fill missing offsets with 0 so the calculation doesn't fail
        csb_corr1['offset_value'] = csb_corr1['offset_value'].fillna(0)
        csb_corr1['depthfinal'] = csb_corr1['depth_new'] - csb_corr1['offset_value']
        csb_corr1['depthfinal'] = csb_corr1['depthfinal'] * -1

        # try to drop some unneeded columns
        csb_corr1 = csb_corr1.drop(
            columns=['s', 'f', 'q', 'DataProv', 'ControlS_2', 'ControlS_1', 'row_id', 'platform_name_y'], errors='ignore')

        print('*****Processed CSB data ready*****')
        # Instead of immediately exporting to geopackage, return the processed GeoDataFrame.
        return csb_corr1

    def rasterize_csb(self,
                      title: str,
                      csb_file: io.File,
                      bag_file: io.File):
        csb_corr1: gpd.GeoDataFrame = self.draft_corr(title, csb_file, bag_file)

        # Insert processed data into DuckDB.
        if self.insert_duckdb:
            self.insert_into_duckdb(csb_corr1)

        # Optionally export as geopackage if the checkbox is selected.
        if self.export_gp:
            gpkg_path: io.File = self.output_dir.new_file(f"csb_processed_{title}.gpkg")
            print('*****Exporting processed CSB data to geopackage*****')
            # First write to memory, then write to file (since GeoPandas can't write to an open file handle)
            buff = BytesIO()
            csb_corr1.to_file(buff, driver='GPKG', layer='csb')
            gpkg_path.write(buff)
            del buff
            print(f"Geopackage exported to {gpkg_path.get_uri()}")

        return csb_corr1

    # --- START: FINAL GRIDDING AND EXPORT FUNCTIONS ---
    def points_to_raster_average(self,
                                 gdf: gpd.GeoDataFrame,
                                 out_raster_path: io.File,
                                 value_col: str = 'depth',
                                 nodata: int = 1000000):
        """
        Creates a GeoTIFF by averaging point values within each grid cell.
        -- MODIFIED for ROBUSTNESS --
        """
        resolution = self.grid_resolution
        if gdf.crs is None:
            raise ValueError("GeoDataFrame has no CRS. Please set or reproject first.")

        x_min, y_min, x_max, y_max = gdf.total_bounds
        width = int(np.ceil((x_max - x_min) / resolution))
        height = int(np.ceil((y_max - y_min) / resolution))

        if width <= 0 or height <= 0:
            print(f"Warning: Raster dimensions are zero or negative for {out_raster_path.object_name}. Skipping.")
            return

        transform = from_origin(x_min, y_max, resolution, resolution)

        sum_array = np.zeros((height, width), dtype=np.float64)  # Use float64 for sums to avoid overflow
        count_array = np.zeros((height, width), dtype=np.int32)

        for geom, value in zip(gdf.geometry, gdf[value_col]):
            if geom is None or pd.isna(value):
                continue
            col = int((geom.x - x_min) // resolution)
            row = int((y_max - geom.y) // resolution)
            if 0 <= col < width and 0 <= row < height:
                sum_array[row, col] += value
                count_array[row, col] += 1

        # method to calculate the average and avoid division by zero
        # Create an output array filled with the nodata value by default
        avg_array = np.full((height, width), nodata, dtype=np.float32)

        # Create a boolean mask of cells where we have data (count > 0)
        valid_mask = count_array > 0

        # Perform the division ONLY on the valid cells and place the results in the output array
        avg_array[valid_mask] = sum_array[valid_mask] / count_array[valid_mask]

        with rasterio.open(
                out_raster_path.open(mode='wb'), mode='w', driver='GTiff',
                height=height, width=width, count=1,
                dtype=np.float32, crs=gdf.crs.to_string(),
                transform=transform, nodata=nodata,
                compress='lzw'
        ) as dst:
            dst.write(avg_array, 1)

        print(f"Final gridded GeoTIFF created at {out_raster_path}")

    def organize_by_epsg(self, input_dir: io.StorageLocation):
        """
        Moves TIFF files into subdirectories named by their EPSG code.
        """
        print("\nOrganizing final GeoTIFFs by EPSG code...")
        for fn in input_dir.list_files(suffix='.tif*'):

            try:
                with rasterio.open(fn.open(mode='rb'), driver='GTiff') as src:
                    epsg = src.crs.to_epsg()
            except Exception as e:
                print(f"[ERROR] could not open {fn.get_uri()}: {e}")
                continue

            if epsg is None:
                print(f"[WARN] {fn.get_uri()} has no recognized EPSG code, skipping.")
                continue

            out_folder: io.StorageLocation = input_dir.sub_location(f"EPSG_{epsg}")
            fn.move(out_folder)
            print(f"Moved {fn.get_uri()} → {out_folder.get_uri()}")

    @staticmethod
    def create_vrts_and_ovr(working_path: Path, vrt_name: str, gdal_paths: list[str], *,
                            ctx_path: Path | None = None) -> tuple[Path, Path]:
        """
        Create VRT and overviews

        Parameters
        ----------
        working_path
            Path representing the directory in which to create VRT
        vrt_name
            Name of VRT file to create
        gdal_paths
            List of strings representing GDAL VSI paths (or relative file paths, i.e., file names)
        ctx_path
            Path to change the current directory to before creating VRT or overviews. If none, the current directory
            will be used.

        Returns
        -------
        Tuple[Path of VRT, Path of VRT overviews]
        """
        if ctx_path:
            cm = contextlib.chdir(ctx_path)
        else:
            cm = contextlib.nullcontext()
        with cm:
            vrt_path = working_path / vrt_name
            gdal.BuildVRT(vrt_path, gdal_paths, strict=True)
            print("Building overviews for VRT...")
            ds = gdal.Open(vrt_path)
            if ds:
                gdal.SetConfigOption('COMPRESS_OVERVIEW', 'LZW')
                ds.BuildOverviews("AVERAGE", [2, 4, 8, 16, 32, 64])
                ds = None
            ovr_name = f"{vrt_name}.ovr"
            ovr_path = working_path / ovr_name
            return vrt_path, ovr_path

    def create_vrts_for_epsg_folders(self, base_dir: io.StorageLocation):
        """
        Scans for 'EPSG_' subfolders and builds a VRT for the TIFFs in each.
        """
        print("\nBuilding VRTs for each EPSG folder...")
        for directory in base_dir.list_sub_paths(pattern='EPSG_'):
            files = directory.list_files(suffix='.tif*')
            print(f"Found {len(files)} files in {directory.get_uri()}. Building VRT...")
            vrt_name = f"mosaic_{directory.name}.vrt"
            # Get GDAL-compatible path to each file
            gdal_paths = [f.get_gdal_vsi_path(relative=True) for f in files]
            # First, create VRT in a temporary file
            if directory.provider_type == io.StorageProviderType.LOCAL_FILE:
                # Create temporary VRT in directory so that BuildVRT will use relative paths
                vrt_path = Path(directory.location)
                ctx_path = vrt_path
            else:
                vrt_path = self.tmp_dir
                ctx_path = None
            tmp_vrt, tmp_ovr = Processor.create_vrts_and_ovr(vrt_path, vrt_name, gdal_paths, ctx_path=ctx_path)
            # Now, copy temporary VRT to directory
            vrt_file = directory.new_file(vrt_name)
            if str(tmp_vrt) != vrt_file.get_uri():
                # Only copy tmp_vrt to vrt_file if they are not already the same file
                with tmp_vrt.open(mode='r') as r:
                    with vrt_file.open(mode='w') as w:
                        w.write(r.read())
            print(f"VRT created: {vrt_file.get_uri()}")
            # Copy overviews to directory
            if not tmp_ovr.exists():
                raise ProcessingException(f"Expected .ovr to exist for VRT {str(tmp_vrt)}, but it did not.")
            ovr_file = directory.new_file(tmp_ovr.name)
            if str(tmp_ovr) != ovr_file.get_uri():
                # Only copy tmp_ovr to ovr_file if they are not already the same file
                with tmp_ovr.open(mode='rb') as r:
                    with ovr_file.open(mode='wb') as w:
                        w.write(r.read())
            print(f"Overviews built for {vrt_file.get_uri()}")

    def run_final_gridding_and_export(self):
        """
        Main function for the final gridding and export stage.
        """
        print("\n***** Starting Final Gridding & Export Stage *****")
        with duckdb.connect(database=self.duckdb_path, read_only=False) as con:
            if self.tessellation_shp is not None and self.tessellation_shp.exists():
                print(f"Using tessellation scheme: {self.tessellation_shp.get_uri()}")
                polygons_gdf = gpd.read_file(self.tessellation_shp.open(mode='rb'))
                if polygons_gdf.crs.to_epsg() != 4326:
                    polygons_gdf = polygons_gdf.to_crs(epsg=4326)

                for idx, poly_row in polygons_gdf.iterrows():
                    poly_geom = poly_row.geometry
                    polygon_id = str(poly_row.get('GRID_ID', idx))
                    print(f"\n--- Processing Tile: {polygon_id} ---")

                    minx, miny, maxx, maxy = poly_geom.bounds
                    query = f"""
                        SELECT lat, lon, "Outlier" AS outlier, depth_mod AS depth, unique_id,
                        platform_name_x as platform_name, time, uncertainty_vert
                        FROM csb
                        WHERE (lat BETWEEN {miny} AND {maxy} AND lon BETWEEN {minx} AND {maxx})
                        AND (Raster_Value IS NULL OR ABS(Raster_Value - depth_mod) <= (uncertainty_vert * 3.5))
                    """
                    df_points = con.execute(query).df()

                    print(f"  Found {len(df_points)} points within the bounding box that passed the quality filter.")

                    if df_points.empty:
                        continue

                    points_gdf_4326 = gpd.GeoDataFrame(
                        df_points,
                        geometry=[Point(xy) for xy in zip(df_points.lon, df_points.lat)],
                        crs="EPSG:4326"
                    )

                    points_gdf_4326 = points_gdf_4326[points_gdf_4326.geometry.within(poly_geom)]

                    print(f"  {len(points_gdf_4326)} points remain after precise clipping to the polygon.")

                    if points_gdf_4326.empty:
                        continue

                    if self.export_final_gpkg:
                        gpkg_path: io.File = self.final_products_loc.new_file(f"{polygon_id}_points.gpkg")
                        print(f"  Saving {len(points_gdf_4326)} points to GeoPackage...")
                        # First write to memory, then write to file (since GeoPandas can't write to an open file handle)
                        buff = BytesIO()
                        points_gdf_4326.to_file(buff, driver="GPKG")
                        gpkg_path.write(buff)
                        del buff
                        print(f"  Saved points GeoPackage (EPSG:4326): {gpkg_path.get_uri()}")

                    lat_c, lon_c = poly_geom.centroid.y, poly_geom.centroid.x
                    try:
                        # --- UPDATED FUNCTION CALL in tiled workflow ---
                        epsg_zone = get_utm_zone_wgs84(lat_c, lon_c)
                        points_gdf_utm = points_gdf_4326.to_crs(epsg=epsg_zone)

                        points_for_raster = points_gdf_utm[points_gdf_utm['outlier'] == False]

                        print(f"  Found {len(points_for_raster)} non-outlier points to create raster from.")

                        if not points_for_raster.empty:
                            tif_path: io.File = self.final_products_loc.new_file(f"{polygon_id}_gridded.tif")
                            self.points_to_raster_average(points_for_raster, tif_path, value_col='depth')
                    except ValueError as e:
                        print(f"  Skipping raster for {polygon_id}: {e}")
                        continue

            else:  # This is the case for a single file output
                print("No tessellation scheme provided. Processing all data into a single file.")
                query = """
                        SELECT lat, \
                               lon, \
                               "Outlier"       AS outlier, \
                               depth_mod       AS depth, \
                               unique_id,
                               platform_name_x as platform_name, time, uncertainty_vert
                        FROM csb
                        WHERE
                            Raster_Value IS NULL
                           OR
                            ABS(Raster_Value - depth_mod) <= (uncertainty_vert * 3.5) \
                        """
                df_points = con.execute(query).df()

                print(f"Initial query returned {len(df_points)} points from the database.")

                if df_points.empty:
                    print("No points passed the quality filter. Aborting final gridding.")
                    print("This can happen if all points intersected reference data but failed the quality check.")
                    return

                points_gdf_4326 = gpd.GeoDataFrame(
                    df_points,
                    geometry=[Point(xy) for xy in zip(df_points.lon, df_points.lat)],
                    crs="EPSG:4326"
                )

                if self.export_final_gpkg:
                    gpkg_path: io.File = self.final_products_loc.new_file('csb_final_points.gpkg')
                    print(f"Saving {len(points_gdf_4326)} points to GeoPackage...")
                    # First write to memory, then write to file (since GeoPandas can't write to an open file handle)
                    buff = BytesIO()
                    points_gdf_4326.to_file(buff, driver="GPKG")
                    gpkg_path.write(buff)
                    del buff
                    print(f"Saved final points GeoPackage (EPSG:4326): {gpkg_path.get_uri()}")

                try:
                    world_centroid = points_gdf_4326.unary_union.centroid
                    # --- THIS IS THE UPDATED FUNCTION CALL ---
                    epsg_code = get_utm_zone_wgs84(world_centroid.y, world_centroid.x)
                    print(f"Determined overall EPSG zone for raster as: {epsg_code}")
                    points_gdf_utm = points_gdf_4326.to_crs(epsg=epsg_code)

                    points_for_raster = points_gdf_utm[points_gdf_utm['outlier'] == False]

                    print(f"Found {len(points_for_raster)} non-outlier points to create raster from.")

                    if not points_for_raster.empty:
                        tif_path: io.File = self.final_products_loc.new_file('csb_final_gridded.tif')
                        self.points_to_raster_average(points_for_raster, tif_path, value_col='depth')
                    else:
                        print("No valid non-outlier points to create final raster.")

                except ValueError as e:
                    print(f"Could not process single file output: {e}")

        if self.organize_vrt:
            self.organize_by_epsg(self.final_products_loc)
            self.create_vrts_for_epsg_folders(self.final_products_loc)

        print("***** Final Gridding & Export Stage Complete *****")

    # --- END: FINAL GRIDDING AND EXPORT FUNCTIONS ---

    # --- START: POST-PROCESSING ANALYSIS FUNCTIONS ---

    def run_histograms_calibration_points(self, db_path, hist_export_dir: io.StorageLocation):
        print("Starting Post-Processing Step 1: Histograms and Calibration Points")

        with duckdb.connect(database=db_path, read_only=False) as con:
            columns_query = "DESCRIBE csb"
            columns_df = con.execute(columns_query).fetchdf()
            if 'diff' in columns_df['column_name'].values:
                print("Dropping incorrect 'diff' column in DuckDB...")
                con.execute("ALTER TABLE csb DROP COLUMN diff")

            columns_df = con.execute(columns_query).fetchdf()
            if 'diff' not in columns_df['column_name'].values:
                print("Creating 'diff' column in DuckDB with correct calculation...")
                con.execute("ALTER TABLE csb ADD COLUMN diff DOUBLE DEFAULT NULL")
                con.execute("UPDATE csb SET diff = (depth_new *-1 - Raster_Value)*-1 WHERE diff IS NULL")

            unique_ids_query = "SELECT DISTINCT unique_id FROM csb"
            unique_ids = con.execute(unique_ids_query).fetchdf()['unique_id']

            for unique_id in unique_ids:
                print(f'Calculating offset histogram for {unique_id}')
                data_query = f"""
                SELECT unique_id, platform_name_x, diff, lon, lat
                FROM csb
                WHERE unique_id = '{unique_id}' 
                  AND diff > -12 AND diff < 12 
                  AND Raster_Value > -20 
                  AND Uncertainty_Value < 3
                """
                data_df = con.execute(data_query).fetchdf()

                if not data_df.empty:
                    platform_name = data_df['platform_name_x'].iloc[0]
                    output_csv_path: io.File = hist_export_dir.new_file(f"{unique_id}_csb_offset_analysis.csv")
                    data_df.to_csv(output_csv_path.open(mode='wb'))

                    plt.figure(figsize=(10, 6))
                    sns.histplot(data_df['diff'], bins=30, kde=True, color="skyblue", label='Histogram')
                    plt.axvline(data_df['diff'].mean(), color='green', linestyle='--',
                                label=f'Mean: {data_df["diff"].mean():.2f}')
                    plt.axvline(data_df['diff'].mean() - data_df['diff'].std(), color='purple', linestyle='--',
                                label=f'-1 Std Dev: {(data_df["diff"].mean() - data_df["diff"].std()):.2f}')
                    plt.axvline(data_df['diff'].mean() + data_df['diff'].std(), color='purple', linestyle='--',
                                label=f'+1 Std Dev: {(data_df["diff"].mean() + data_df["diff"].std()):.2f}')
                    plt.text(data_df['diff'].mean() - data_df['diff'].std(), plt.ylim()[1] * 0.95,
                             f'-1 SD: {data_df["diff"].std():.2f}', horizontalalignment='right', color='purple')
                    plt.text(data_df['diff'].mean() + data_df['diff'].std(), plt.ylim()[1] * 0.95,
                             f'+1 SD: {data_df["diff"].std():.2f}', horizontalalignment='left', color='purple')
                    plt.title(f'Distribution of Diff Values for {unique_id} ({platform_name})')
                    plt.xlabel('Diff')
                    plt.ylabel('Frequency')
                    plt.legend()
                    histo_png: io.File = hist_export_dir.new_file(f"{unique_id}_histogram.png")
                    plt.savefig(histo_png.open(mode='wb'))
                    plt.close()

        print("Completed Step 1.")

    def run_apply_best_offsets(self, db_path):
        print("Starting Post-Processing Step 2: Apply Best Offsets")
        with duckdb.connect(database=db_path, read_only=False) as con:
            # Check if columns exist; if not, add them. This is safer for iterative runs.
            columns_df = con.execute("DESCRIBE csb").fetchdf()
            if 'depth_mod' not in columns_df['column_name'].values:
                con.execute("ALTER TABLE csb ADD COLUMN depth_mod DOUBLE;")
            if 'uncertainty_vert' not in columns_df['column_name'].values:
                con.execute("ALTER TABLE csb ADD COLUMN uncertainty_vert DOUBLE;")
            if 'uncertainty_hori' not in columns_df['column_name'].values:
                con.execute("ALTER TABLE csb ADD COLUMN uncertainty_hori DOUBLE;")

            print("Applying best offsets to new data...")
            con.execute("""
                        UPDATE csb
                        SET depth_mod = (depth_new - sub.average_diff) * -1 FROM (
                SELECT unique_id, AVG(diff) AS average_diff
                FROM csb
                WHERE diff > -12 AND diff < 12 AND Raster_Value > -20 AND Uncertainty_Value < 3
                GROUP BY unique_id
            ) AS sub
                        WHERE csb.unique_id = sub.unique_id AND csb.depth_mod IS NULL;
                        """)

            # Fallback for points that couldn't be calibrated against reference data
            update_query = """
                           UPDATE csb
                           SET depth_mod = depthfinal
                           WHERE depth_mod IS NULL; \
                           """
            con.execute(update_query)
            print("Updated remaining depth_mod values with initial depthfinal.")

            # Calculate uncertainty only for new rows ---
            print("Calculating CATZOC uncertainty for new data...")
            uncert_vert_query = "UPDATE csb SET uncertainty_vert = (2 + (depth_mod * -0.05)) WHERE uncertainty_vert IS NULL"
            uncert_hori_query = "UPDATE csb SET uncertainty_hori = 10 WHERE uncertainty_hori IS NULL"
            con.execute(uncert_vert_query)
            con.execute(uncert_hori_query)
            print("Uncertainty values calculated.")

        print("Completed Step 2.")

    def run_export_transits(self, db_path, exports_folder: io.StorageLocation):
        print("Starting Post-Processing Step 3: Outlier Detection & Transit ID Assignment")

        # --- Helper Functions ---
        def haversine(lat1, lon1, lat2, lon2):
            R = 6371000
            phi1, phi2 = np.radians(lat1), np.radians(lat2)
            dphi = np.radians(lat2 - lat1)
            dlambda = np.radians(lon2 - lon1)
            a = np.sin(dphi / 2) ** 2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2) ** 2
            c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))
            return R * c

        def calculate_vessel_speed(group):
            group = group.sort_values('time').copy()
            group['lat'] = pd.to_numeric(group['lat'], errors='coerce')
            group['lon'] = pd.to_numeric(group['lon'], errors='coerce')
            group['time_diff'] = group['time'].diff().dt.total_seconds()
            group['distance'] = haversine(
                group['lat'].shift(), group['lon'].shift(),
                group['lat'], group['lon']
            )
            group['vessel_speed'] = group['distance'] / group['time_diff']
            group['vessel_speed'] = group['vessel_speed'].replace([np.inf, -np.inf], 0)
            group['vessel_speed'] = group['vessel_speed'].fillna(0)
            group['vessel_speed_smoothed'] = uniform_filter1d(group['vessel_speed'], size=5)
            group['vessel_speed_smoothed'] = group['vessel_speed_smoothed'].replace([np.inf, -np.inf], 0)
            return group

        def detect_outliers(data, scaler, threshold_percentile, original_gdf, return_smoothed=False):
            data_scaled = scaler.fit_transform(data)
            imputer = IterativeImputer(
                estimator=LinearRegression(),
                max_iter=15,
                random_state=42,
                sample_posterior=False
            )
            data_imputed = imputer.fit_transform(data_scaled)
            smoothed_depth = uniform_filter1d(data_imputed[:, 2], size=50)
            residuals = np.abs(data_scaled[:, 2] - smoothed_depth)
            threshold = np.percentile(residuals, threshold_percentile)
            outliers = residuals > threshold
            smoothed_depth_denorm = smoothed_depth * scaler.scale_[2] + scaler.mean_[2]
            original_gdf.loc[data.index[outliers], 'Outlier'] = True
            outlier_count = np.sum(outliers)
            if return_smoothed:
                full_smoothed_depth = pd.Series(index=original_gdf.index, dtype=float)
                full_smoothed_depth[data.index] = smoothed_depth_denorm
                return full_smoothed_depth, outlier_count
            return data[~outliers], outlier_count

        def create_geotiff(gdf, filename: io.File, resolution=8):
            try:
                bounds = gdf.total_bounds
                x_min, y_min, x_max, y_max = bounds
                if x_max == x_min or y_max == y_min:
                    raise ValueError("Invalid geographic bounds. All points may be identical or too close.")
                x_res = int((x_max - x_min) / resolution)
                y_res = int((y_max - y_min) / resolution)
                transform = from_origin(x_min, y_max, resolution, resolution)
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
                with rasterio.open(filename.open(mode='wb'), mode='w', **out_meta) as dest:
                    for idx, col in enumerate(['depth', 'uncertainty'], start=1):
                        array = np.full((y_res, x_res), out_meta['nodata'], dtype='float32')
                        for point, value in zip(gdf.geometry, gdf[col]):
                            col_idx = int((point.x - x_min) / resolution)
                            row_idx = int((y_max - point.y) / resolution)
                            if 0 <= col_idx < x_res and 0 <= row_idx < y_res:
                                array[row_idx, col_idx] = value
                        dest.write(array, idx)
            except Exception as e:
                print(f"Failed to create GeoTIFF for {filename}. Error: {str(e)}")

        def create_transit_ids(df, max_hours_gap, max_days_duration):
            df = df.sort_values(by='time')
            current_transit_id = None
            last_time = None
            current_start_time = None
            transit_ids = []
            for index, row in df.iterrows():
                if last_time is None or (row['time'] - last_time > timedelta(hours=max_hours_gap)) \
                        or ((row['time'] - current_start_time) > timedelta(days=max_days_duration)):
                    current_transit_id = f"{row['unique_id']}_{row['time'].strftime('%Y-%m-%d_%H-%M-%S')}"
                    current_start_time = row['time']
                transit_ids.append(current_transit_id)
                last_time = row['time']
            df['transit_id'] = transit_ids
            return df

        MAX_HOURS_GAP = 4
        MAX_DAYS_DURATION = 7
        with duckdb.connect(database=db_path, read_only=False) as con:
            con.install_extension('spatial')
            con.load_extension('spatial')

            # Ensure columns exist
            columns_df = con.execute("DESCRIBE csb").fetchdf()
            if 'Outlier' not in columns_df['column_name'].values:
                con.execute("ALTER TABLE csb ADD COLUMN Outlier BOOLEAN DEFAULT FALSE;")
            if 'transit_id' not in columns_df['column_name'].values:
                con.execute("ALTER TABLE csb ADD COLUMN transit_id VARCHAR;")
            if 'vessel_speed_smoothed' not in columns_df['column_name'].values:
                con.execute("ALTER TABLE csb ADD COLUMN vessel_speed_smoothed DOUBLE;")

            # Get unique_ids ONLY from unprocessed data
            unprocessed_ids_query = "SELECT DISTINCT unique_id FROM csb WHERE transit_id IS NULL"
            unique_ids = con.execute(unprocessed_ids_query).fetchall()
            unique_ids = [x[0] for x in unique_ids]

            if not unique_ids:
                print("No new data to process for outlier detection. Skipping.")
                print("Completed Step 3.")
                return

            print(f"Found {len(unique_ids)} unique_id(s) with new data to process.")

            for i, unique_id in enumerate(unique_ids, start=1):
                print(f"\nProcessing new data for unique_id {i}/{len(unique_ids)}: {unique_id}")
                # Query ONLY unprocessed data for the current vessel
                query = f"""
                SELECT
                    rowid, unique_id, platform_name_x AS platform_name, time,
                    depth_mod AS depth, uncertainty_vert AS uncertainty, uncertainty_hori,
                    lat, lon, Raster_Value
                FROM csb
                WHERE unique_id = '{unique_id}'
                  AND depth_mod IS NOT NULL
                  AND transit_id IS NULL
                ORDER BY time;
                """
                df = con.execute(query).df()
                if df.empty:
                    print(f"  No new data with depth_mod for {unique_id}, skipping.")
                    continue

                df['time'] = pd.to_datetime(df['time'], format='%Y%m%d %H:%M:%S')
                df = create_transit_ids(df, MAX_HOURS_GAP, MAX_DAYS_DURATION)
                for transit_id, group in df.groupby('transit_id'):
                    print(f"  Processing transit: {transit_id}")
                    group = group.sort_values('time').copy()
                    group['lat'] = pd.to_numeric(group['lat'], errors='coerce')
                    group['lon'] = pd.to_numeric(group['lon'], errors='coerce')
                    group['Outlier'] = False
                    group = calculate_vessel_speed(group)
                    excess_speed_points = group[group['vessel_speed_smoothed'] > 10.3]
                    print(f"    {len(excess_speed_points)} points exceed 20 knots (not filtered out).")
                    data_for_outlier = group[['lat', 'lon', 'depth']].copy()
                    scaler = StandardScaler()
                    print("    First Pass (99th percentile):")
                    filtered_data_1, outlier_count_1 = detect_outliers(data_for_outlier.copy(), scaler, 99,
                                                                       original_gdf=group)
                    print(f"    Outliers detected in Pass 1: {outlier_count_1}")
                    print("    Second Pass (98th percentile):")
                    filtered_data_2, outlier_count_2 = detect_outliers(filtered_data_1.copy(), scaler, 98,
                                                                       original_gdf=group)
                    print(f"    Outliers detected in Pass 2: {outlier_count_2}")
                    print("    Third Pass (98th percentile, strict):")
                    final_smoothed_depth, outlier_count_3 = detect_outliers(filtered_data_2.copy(), scaler, 98,
                                                                            original_gdf=group, return_smoothed=True)
                    print(f"    Outliers detected in Pass 3: {outlier_count_3}")
                    group['Final_Smoothed_Depth'] = final_smoothed_depth

                    plot_filename: io.File = exports_folder.new_file(f"{unique_id}_{transit_id}_outlier_plot.png")
                    fig, ax = plt.subplots(figsize=(12, 6))
                    mask_valid = group['Outlier'] == False
                    mask_outlier = group['Outlier'] == True
                    ax.scatter(group.index[mask_valid], group.loc[mask_valid, 'depth'],
                               color='blue', s=1, label="Valid")
                    ax.scatter(group.index[mask_outlier], group.loc[mask_outlier, 'depth'],
                               color='red', s=10, label="Outlier")
                    ax.set_title(f"Outlier Detection for Transit {transit_id} (unique_id: {unique_id})")
                    ax.set_xlabel("Record Index")
                    ax.set_ylabel("Depth")
                    ax.legend()
                    plt.savefig(plot_filename.open(mode='wb'))
                    plt.close()
                    print(f"    Saved outlier plot to {plot_filename.get_uri()}")

                    updates = []
                    for idx, row in group.iterrows():
                        updates.append((row['rowid'], row['transit_id'], str(row['Outlier']).upper()))
                    if updates:
                        updates_df = pd.DataFrame(updates, columns=['rowid', 'transit_id', 'outlier'])
                        con.register('updates_df', updates_df)
                        con.execute("""
                                    UPDATE csb
                                    SET transit_id = updates_df.transit_id,
                                        Outlier    = CAST(updates_df.outlier AS BOOLEAN) FROM updates_df
                                    WHERE csb.rowid = updates_df.rowid;
                                    """)
                        con.unregister('updates_df')
                        print(f"    ...updating {len(updates)} rows for Outlier and transit_id.")
                    del updates
                    gc.collect()

                    speed_updates = []
                    for idx, row in group.iterrows():
                        speed_updates.append((row['rowid'], row['vessel_speed_smoothed']))
                    if speed_updates:
                        speed_updates_df = pd.DataFrame(speed_updates, columns=['rowid', 'speed'])
                        con.register('speed_updates_df', speed_updates_df)
                        con.execute("""
                                    UPDATE csb
                                    SET vessel_speed_smoothed = speed_updates_df.speed FROM speed_updates_df
                                    WHERE csb.rowid = speed_updates_df.rowid;
                                    """)
                        con.unregister('speed_updates_df')
                        print(f"    ...updating {len(speed_updates)} rows for vessel_speed_smoothed.")
                    del speed_updates
                    gc.collect()

                    # --- Transit export is optional ---
                    if self.export_transits:
                        gdf = gpd.GeoDataFrame(group, geometry=gpd.points_from_xy(group.lon, group.lat))
                        gdf.set_crs(epsg=4326, inplace=True)
                        if gdf.empty:
                            print(f"No valid points left in transit {transit_id}. Skipping export.")
                            continue

                        non_outlier_gdf = gdf[gdf['Outlier'] == False]
                        if non_outlier_gdf.empty:
                            print(f"No non-outlier points left in transit {transit_id}. Skipping export.")
                            continue

                        avg_lat = non_outlier_gdf['lat'].mean()
                        avg_lon = non_outlier_gdf['lon'].mean()
                        try:
                            epsg_zone = get_utm_zone_nad83(avg_lat, avg_lon)
                        except ValueError as ve:
                            print(f"Could not determine NAD83 UTM zone for lat={avg_lat}, lon={avg_lon}: {ve}")
                            continue

                        zone_folder: io.StorageLocation = exports_folder.sub_location(f"zone_{epsg_zone}")
                        non_outlier_gdf.to_crs(epsg=epsg_zone, inplace=True)

                        start_date = group['time'].min().strftime('%Y%m%d%H%M%S')
                        end_date = group['time'].max().strftime('%Y%m%d%H%M%S')
                        gpkg_filename = f"{unique_id}_{transit_id}_{start_date}_{end_date}.gpkg"
                        gpkg_path: io.File = zone_folder.new_file(gpkg_filename)
                        # First write to memory, then write to file (since GeoPandas can't write to an open file handle)
                        buff = BytesIO()
                        non_outlier_gdf.to_file(buff, driver='GPKG')
                        gpkg_path.write(buff)
                        del buff
                        print(f"Exported GeoPackage {gpkg_path.get_uri()}")

                        tiff_filename = gpkg_filename.replace('.gpkg', '.tif')
                        tiff_path: io.File = zone_folder.new_file(tiff_filename)
                        create_geotiff(non_outlier_gdf, tiff_path)
                        print(f"Exported GeoTIFF {tiff_path.get_uri()}")
                        del gdf

                    del group
                    gc.collect()

        print("Completed Step 3.")

    # --- END: POST-PROCESSING ANALYSIS FUNCTIONS ---

    def run(self, *,
            clean_tmp_on_exit: bool = True):
        try:
            start_time = time.time()
            final_products: io.StorageLocation = self.output_dir.sub_location('final_products')

            for csb_file in self.csb_directory.list_files(suffix='.csv'):
                title = csb_file.get_stem()
                # We check for a final product to determine if we should skip
                if final_products.contains('csb_final_gridded.tif'):
                    print(f"Skipping already processed file based on existing final products: {title}")
                    continue

                print(f"Processing {csb_file.get_uri()} with title: {title}")
                try:
                    self.output_dir.delete_all('Modeling')
                    if self.use_bluetopo:
                        bag_file: io.File = self.create_convex_hull_and_download_tiles(title, csb_file)
                    elif self.bag_file_path:
                        bag_file: io.File = self.bag_file_path
                    else:
                        raise Exception(f"Neither bag_file_path nor use_bluetopo where set when one must be.")

                    self.rasterize_csb(title, csb_file, bag_file)
                except Exception as e:
                    tb.print_exception(e)
                    raise ProcessingException(f"An error occurred during initial processing of {csb_file}: {e}")

            if self.run_analysis:
                print("\n***** Starting Post-Processing Analysis *****")
                hist_export_dir: io.StorageLocation = self.output_dir.sub_location('histograms')
                exports_folder: io.StorageLocation = self.output_dir.sub_location('transit_exports')

                if not self.duckdb_path.exists():
                    print(f"Error: DuckDB file not found at {str(self.duckdb_path)}. Cannot run analysis.")
                else:
                    self.run_histograms_calibration_points(self.duckdb_path, hist_export_dir)
                    self.run_apply_best_offsets(self.duckdb_path)
                    self.run_export_transits(self.duckdb_path, exports_folder)  # This now checks internally if it should run

            if self.run_final_grid:
                self.run_final_gridding_and_export()

            end_time = time.time()
            duration = end_time - start_time
            minutes, seconds = divmod(duration, 60)
            print(f"***** ALL STAGES DONE! Total processing time: {int(minutes)} minutes and {seconds:.1f} seconds")
            print("processing complete.")
            if self.clean_up_callback:
                print("calling clean-up callback")
                self.clean_up_callback()
        finally:
            if clean_tmp_on_exit:
                shutil.rmtree(self.tmp_dir)
            else:
                print(f"Preserving temporary directory {self.tmp_dir}...")
