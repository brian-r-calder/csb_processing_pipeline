import os
from datetime import datetime
import logging
import sys
import shutil
import glob
import time
from typing import Callable

import requests
import geopandas as gpd
import matplotlib
import numpy as np
import pandas as pd
import rasterio
from osgeo import gdal
from rasterio.features import shapes
from rasterio.warp import Resampling
from shapely.geometry import shape
from shapely.validation import make_valid
import duckdb

matplotlib.use('Agg')
from shapely.ops import unary_union
from skimage.morphology import binary_dilation, binary_erosion
from scipy.interpolate import interp1d

from fes_model import get_fes_tide, get_lat_separation


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


class Processor:
    def __init__(self,
                 csb_directory: str,
                 fp_zones: str,
                 bag_file_path: str,
                 output_dir: str,
                 *,
                 clean_up_callback: Callable|None = None,
                 use_bluetopo: bool = True,
                 use_fes_model: bool = True,
                 fes_data_path: str|None = None,
                 fes_yaml_path: str|None = None,
                 run_analysis: bool = False,
                 run_final_grid: bool = False,
                 export_gp: bool = False,
                 insert_duckdb: bool = False):
        self.title = ''
        self.csb_directory = os.path.abspath(csb_directory)
        self.fp_zones = os.path.abspath(fp_zones)
        self.bag_file_path = os.path.abspath(bag_file_path)
        self.output_dir = os.path.abspath(output_dir)
        self.clean_up_callback = clean_up_callback
        self.use_bluetopo = use_bluetopo
        self.use_fes_model = use_fes_model
        self.fes_data_path = fes_data_path
        self.fes_yaml_path = fes_yaml_path
        self.run_analysis = run_analysis
        self.run_final_grid = run_final_grid
        self.export_gp = export_gp
        self.insert_duckdb = insert_duckdb

        # setup_logging(output_dir)
        print(f"output_dir is: {output_dir}")

    def load_csb(self, csb_file: str):
        print('*****Reading CSB input csv file in chunks*****')

        chunk_size = 5_000_000
        processed_chunks = []

        # This creates an iterator that yields a DataFrame chunk on each loop
        with pd.read_csv(csb_file, chunksize=chunk_size, low_memory=False) as reader:
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
                                              csb_data_path, output_dir,
                                              title: str,
                                              bag_file_path: str,
                                              use_bluetopo=True):
        """
        Loads CSB data, builds a convex hull, and writes it to a shapefile.
        If use_bluetopo is True, it creates a dedicated Modeling folder,
        downloads BlueTopo tiles, copies them to a separate archive folder,
        and then builds a VRT from all GeoTIFF files found recursively in that folder.
        If use_bluetopo is False, it returns the user‐provided BAG_filepath.
        """

        # Load CSB data and create convex hull
        csb_data = pd.read_csv(csb_data_path)
        gdf = gpd.GeoDataFrame(csb_data, geometry=gpd.points_from_xy(csb_data.lon, csb_data.lat))
        gdf = gdf.set_crs(4326, allow_override=True)
        convex_hull_polygon = gdf.unary_union.convex_hull
        convex_hull_gdf = gpd.GeoDataFrame(geometry=[convex_hull_polygon], crs=gdf.crs)
        convex_hull_shapefile = os.path.join(output_dir, "convex_hull_polygon.shp")
        convex_hull_gdf.to_file(convex_hull_shapefile)
        print(f"Convex hull shapefile written to: {convex_hull_shapefile}")

        if use_bluetopo:
            # Create the 'Modeling' folder for downloading tiles
            bluetopo_tiles_dir = os.path.join(output_dir, "Modeling")
            os.makedirs(bluetopo_tiles_dir, exist_ok=True)

            # Download BlueTopo tiles
            from nbs.bluetopo import fetch_tiles
            fetch_tiles(bluetopo_tiles_dir, convex_hull_shapefile, data_source='modeling')

            # Use the CSV file's title (assumed to be stored in the global variable 'title')
            bluetopo_tiles_copy = os.path.join(output_dir, f"BlueTopo_Tiles_{title}")
            if os.path.exists(bluetopo_tiles_copy):
                shutil.rmtree(bluetopo_tiles_copy)
            shutil.copytree(bluetopo_tiles_dir, bluetopo_tiles_copy)
            print(f"Copied BlueTopo tiles to {bluetopo_tiles_copy}")

            # Build a VRT from the copied tiles using a unique folder name that includes the title.
            vrt_dir = os.path.join(output_dir, f"BlueTopo_VRT_{title}")
            os.makedirs(vrt_dir, exist_ok=True)

            # Create glob patterns to find both .tif and .tiff files in the unique tiles folder.
            tif_pattern = os.path.join(bluetopo_tiles_copy, '**', '*.tif')
            tiff_pattern = os.path.join(bluetopo_tiles_copy, '**', '*.tiff')
            print(f"[DEBUG] Glob pattern for .tif: {tif_pattern}")
            print(f"[DEBUG] Glob pattern for .tiff: {tiff_pattern}")
            tile_files = glob.glob(tif_pattern, recursive=True) + glob.glob(tiff_pattern, recursive=True)
            print("[DEBUG] Found the following tile files for VRT building:")
            for f in tile_files:
                print("  ", f)

            if not tile_files:
                raise RuntimeError("No BlueTopo GeoTIFF files were found in " + bluetopo_tiles_copy)

            # Save the VRT file with the title appended to the file name.
            vrt_path = os.path.join(vrt_dir, f"merged_tiles_{title}.vrt")
            vrt = gdal.BuildVRT(vrt_path, tile_files)
            vrt = None  # Close the VRT dataset
            print(f"Created VRT at {vrt_path}")
            return vrt_path
        else:
            # If not using automated download, assume bag_file_path is provided by the user.
            return bag_file_path

    def read_master_offsets(self):
        """Reads the master offsets from a CSV file."""
        MASTER_OFFSET_FILE = os.path.join(self.output_dir, "master_offsets.csv")

        if os.path.exists(MASTER_OFFSET_FILE):
            return pd.read_csv(MASTER_OFFSET_FILE)
        else:
            return pd.DataFrame(columns=['unique_id', 'platform_name', 'offset_value', 'std_dev', 'accuracy_score', 'date_range', 'tile_name'])

    def update_master_offsets(self,
                              unique_id, platform_name, new_offset, std_dev, date_range):
        global MASTER_OFFSET_FILE
        MASTER_OFFSET_FILE = os.path.join(self.output_dir, "master_offsets.csv")
        master_offsets = self.read_master_offsets()


        accuracy_score = 1 / std_dev if std_dev != 0 else 0

        #print('checking for existing offset by unique_id and platform_name')
        existing_index = master_offsets[(master_offsets['unique_id'] == unique_id)].index

        new_row = pd.DataFrame([{
            'unique_id': unique_id,
            'platform_name': platform_name,
            'offset_value': new_offset,
            'std_dev': std_dev,
            'accuracy_score': accuracy_score,
            'date_range': date_range,
            'tile_name': self.title
        }])

        # Exclude empty or all-NA entries before concatenation
        new_row = new_row.dropna(how='all')

        if existing_index.empty:
            master_offsets = pd.concat([master_offsets, new_row], ignore_index=True)
        else:
            if master_offsets.loc[existing_index[0], 'accuracy_score'] <= accuracy_score:
                master_offsets.loc[existing_index[0], list(new_row.columns)] = new_row.iloc[0]

        try:
            master_offsets.to_csv(MASTER_OFFSET_FILE, index=False)
            #print(f"Master offsets updated successfully in {MASTER_OFFSET_FILE}.")
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
                              raster_path, output_dir, title, desired_resolution=8, dilation_iterations=3, erosion_iterations=2):
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
                binary_mask = binary_dilation(binary_mask)
            for _ in range(erosion_iterations):
                binary_mask = binary_erosion(binary_mask)

            # Ensure binary_mask is of type uint8
            binary_mask = binary_mask.astype(np.uint8)

            # Generate shapes from the binary mask
            transform = raster.transform * raster.transform.scale(
                (raster.width / data.shape[-1]),
                (raster.height / data.shape[-2])
            )

            #polygons = [shape(geom) for geom, val in shapes(binary_mask, mask=binary_mask, transform=transform) if val == 1]

            # Perform unary union
            #unified_geometry = unary_union(polygons)
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
            bathy_polygon_shp = f"{output_dir}/{title}_bathy_polygon.shp"
            geo_df.to_file(bathy_polygon_shp, driver='ESRI Shapefile')

            print('Bathymetry polygon shapefile created.')
            return bathy_polygon_shp

    def apply_fes_tides(self, gdf):
        """
        Applies tides using the FES global model and transforms the result
        directly to be referenced to Lowest Astronomical Tide (LAT).
        """
        print("***** Applying tides using global FES model (referenced to LAT) *****")

        if not self.fes_data_path or not self.fes_yaml_path:
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

    def tides(self, csb_file: str):
        gdf = self.load_csb(csb_file)

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

            zones = gpd.read_file(self.fp_zones)
            join = gpd.sjoin(gdf, zones, how='inner', predicate='within')
            join = join.astype({'time': 'datetime64[ns]'})
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

    def extract_bag(self, bag_file):
        print("starting BAGextract() function")
        BAG_filepath = os.path.abspath(bag_file)
        print("DEBUG - BAG_filepath:", bag_file)
        print('*****Starting to import reference bathy*****')

        output_raster_wgs84 = os.path.join(self.output_dir, self.title + '_wgs84.tif')
        temp_vrt_path = os.path.join(self.output_dir, 'temp_for_warp.vrt')

        print("Warping input raster to standard WGS84 (EPSG:4269)...")

        input_for_warp = BAG_filepath

        # For BAG files, we first create a VRT to select the depth and uncertainty bands
        if BAG_filepath.lower().endswith('.bag'):
            print("BAG file detected, creating temporary VRT to select bands 1 and 2...")
            # gdal.BuildVRT is the correct place to use bandList
            gdal.BuildVRT(temp_vrt_path, BAG_filepath, bandList=[1, 2])
            input_for_warp = temp_vrt_path

        # BIGTIFF=YES' to creationOptions to allow files larger than 4GB
        gdal.Warp(output_raster_wgs84, input_for_warp,
                  dstSRS='EPSG:4326',
                  creationOptions=['COMPRESS=LZW', 'BIGTIFF=YES'],
                  dstNodata=1000000)

        # Clean up the temporary VRT file if it was created
        if os.path.exists(temp_vrt_path):
            os.remove(temp_vrt_path)

        print("Reference raster prepared successfully.")

        # Call create_survey_outline to generate the bathymetry polygon shapefile
        bathy_polygon_shp = self.create_survey_outline(output_raster_wgs84, self.output_dir, self.title)

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

    def derive_draft(self, csb_file, bag_file, master_offsets_df, report=None):  # Added report default for safety
        output_raster, raster_boundary_shp = self.extract_bag(bag_file)
        csb_corr = self.tides(csb_file)

        vessels_with_offsets = master_offsets_df['unique_id'].unique().tolist()
        if report: report.add_statistic("Vessels with existing offsets", len(vessels_with_offsets))

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
                out.to_csv(os.path.join(self.output_dir, 'VESSEL_OFFSETS_csb_corr_' + self.title + '.csv'), mode='a')

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
                    self.update_master_offsets(unique_id, platform_name, new_offset, std_dev, date_range, self.title)
            except Exception as e:
                print(f"Unexpected error encountered creating aggregation dataframe: {e}")

        return csb_corr

    def insert_into_duckdb(self, gdf, duckdb_path):
        """
        Inserts (or appends) data, automatically upgrading the table schema if necessary.
        -- FINAL, ROBUST VERSION --
        """
        # Ensure the directory for the DuckDB file exists.
        duckdb_dir = os.path.dirname(duckdb_path)
        if not os.path.exists(duckdb_dir):
            os.makedirs(duckdb_dir, exist_ok=True)

        df = gdf.copy()
        if 'geometry' in df.columns:
            df['wkb_geom'] = df['geometry'].apply(lambda geom: geom.wkb if geom is not None else None)
            df = df.drop(columns='geometry')
        else:
            df['wkb_geom'] = None

        # This master list defines the complete, final schema.
        master_columns_with_types = {
            "ControlStn": "VARCHAR", "Raster_Value": "DOUBLE", "Uncertainty_Value": "DOUBLE",
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
            with duckdb.connect(database=duckdb_path, read_only=False) as con:
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
                    columns_for_create = ", ".join(
                        [f"{name} {dtype}" for name, dtype in master_columns_with_types.items()])
                    con.execute(f"CREATE TABLE csb ({columns_for_create})")

                # Now, the table schema is guaranteed to match our DataFrame.
                # Use an explicit column list in the INSERT statement for maximum safety.
                col_list_for_insert = ", ".join(master_columns)
                con.register("temp_df", df)
                con.execute(f"INSERT INTO csb ({col_list_for_insert}) SELECT * FROM temp_df")

            print(f"Data successfully appended to DuckDB table at {duckdb_path}.")
        except Exception as e:
            print(f"CRITICAL ERROR inserting into DuckDB: {e}")
            raise e

    def draft_corr(self, csb_file, bag_file, master_offsets_df):
        csb_corr = self.derive_draft(csb_file, bag_file, master_offsets_df)

        # Merge the CSB data with the master offsets based on unique vessel ID
        # This will now include any newly derived offsets from the step above
        master_offsets_updated = self.read_master_offsets()  # Read the potentially updated file
        csb_corr1 = csb_corr.merge(master_offsets_updated, on='unique_id', how='left')

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

    def rasterize_csb(self, csb_file, bag_file, master_offsets_df):
        csb_corr1 = self.draft_corr(csb_file, bag_file, master_offsets_df)

        # Based on GUI options, insert processed data into DuckDB.
        if self.insert_duckdb:
            duckdb_path = os.path.join(self.output_dir, "csb.duckdb")
            self.insert_into_duckdb(csb_corr1, duckdb_path)

        # Optionally export as geopackage if the checkbox is selected.
        if self.export_gp:
            gpkg_path = os.path.join(self.output_dir, 'csb_processed_' + self.title + '.gpkg')
            print('*****Exporting processed CSB data to geopackage*****')
            csb_corr1.to_file(gpkg_path, driver='GPKG', layer='csb')
            print(f"Geopackage exported to {gpkg_path}")

        return csb_corr1

    def run(self):
        start_time = time.time()
        # --- Load master offsets once at the start ---
        MASTER_OFFSET_FILE = os.path.join(self.output_dir, "master_offsets.csv")
        if os.path.exists(MASTER_OFFSET_FILE):
            print(f"Found existing master offsets file at: {MASTER_OFFSET_FILE}")
            master_offsets_df = pd.read_csv(MASTER_OFFSET_FILE)
        else:
            print("No master_offsets.csv found. Will create a new one.")
            master_offsets_df = pd.DataFrame(
                columns=['unique_id', 'platform_name', 'offset_value', 'std_dev', 'accuracy_score', 'date_range',
                         'tile_name'])

        csv_files = [os.path.join(self.csb_directory, f) for f in os.listdir(self.csb_directory) if f.endswith('.csv')]
        for csb_file in csv_files:
            self.title = os.path.splitext(os.path.basename(csb_file))[0]
            # We check for a final product to determine if we should skip
            final_product_check = os.path.join(self.output_dir, "final_products", "csb_final_gridded.tif")
            if os.path.exists(final_product_check):
                print(f"Skipping already processed file based on existing final products: {self.title}")
                continue

            print(f"Processing {csb_file} with title: {self.title}")
            try:
                modeling_dir_path = os.path.join(self.output_dir, "Modeling")
                try:
                    shutil.rmtree(modeling_dir_path)
                except FileNotFoundError:
                    pass  # It's ok if it doesn't exist
                except Exception as e:
                    print(f"Error deleting old Modeling folder: {e}")

                if self.use_bluetopo:
                    bag_file = self.create_convex_hull_and_download_tiles(csb_file,
                                                                              self.output_dir,
                                                                              self.title,
                                                                              self.bag_file_path,
                                                                              use_bluetopo=True)
                else:
                    bag_file = self.bag_file_path

                # ---Pass the master_offsets_df to rasterize_CSB ---
                self.rasterize_csb(csb_file, bag_file, master_offsets_df)

            except Exception as e:
                print(f"An error occurred during initial processing of {csb_file}: {e}")
            finally:
                self.cleanup_interim_files(self.output_dir, self.title)

        if self.run_analysis:
            print("\n***** Starting Post-Processing Analysis *****")
            db_path = os.path.join(self.output_dir, "csb.duckdb")
            hist_export_dir = os.path.join(self.output_dir, "histograms")
            exports_folder = os.path.join(self.output_dir, "transit_exports")

            if not os.path.exists(db_path):
                print(f"Error: DuckDB file not found at {db_path}. Cannot run analysis.")
            else:
                self.run_histograms_calibration_points(db_path, hist_export_dir)
                self.run_apply_best_offsets(db_path)
                self.run_export_transits(db_path, exports_folder)  # This now checks internally if it should run

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
