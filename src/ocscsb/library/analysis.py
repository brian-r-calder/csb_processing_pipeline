from pathlib import Path
from datetime import timedelta
import seaborn as sns
import matplotlib.pyplot as plt
import duckdb
import pandas as pd
import geopandas as gpd
import numpy as np
from sklearn.experimental import enable_iterative_imputer  # this enables the sklearn experimental feature
from sklearn.impute import IterativeImputer
from sklearn.linear_model import LinearRegression
from sklearn.preprocessing import StandardScaler
from scipy.ndimage import uniform_filter1d
from rich import print

from ocscsb.library.database import transit_df, update_db_for_transits
from ocscsb.library.geotiff import create_geotiff

def generate_offset_histograms(db_file: Path, export_dir: Path, **kwargs) -> tuple[int,int]:
    verbose: bool = kwargs.get('verbose', False)

    files_extracted: int = 0
    plots_made: int = 0

    with duckdb.connect(database=db_file.as_posix()) as con:
        # Fetch all unique identifiers
        unique_ids_query = "SELECT DISTINCT unique_id FROM csb"
        unique_ids = con.execute(unique_ids_query).fetchdf()['unique_id']

        for unique_id in unique_ids:
            if verbose:
                print(f'Calculating offset histogram for {unique_id}')
    
            # Fetch data for the current unique_id
            data_query = f"""
            SELECT unique_id, platform_name_x, diff, lon, lat
            FROM csb
            WHERE unique_id = '{unique_id}' AND diff > -12 AND diff < 12 AND Raster_Value > -20 AND Uncertainty_Value < 3
            """
            data_df: pd.DataFrame = con.execute(data_query).fetchdf()
            
            if data_df.empty:
                continue
        
            platform_name = data_df['platform_name_x'].iloc[0] 
                # Assuming platform_name_x is consistent within unique_id
        
            output_csv_path: Path = export_dir / f'{unique_id}_csb_offset_analysis.csv'
            output_plot_path: Path = export_dir / f'{unique_id}_histogram.png'
            
            data_df.to_csv(output_csv_path)
            files_extracted += 1

            # Plotting the histogram and density plot for 'diff'
            plt.figure(figsize=(10, 6))
            sns.histplot(data_df['diff'].tolist(), bins=30, kde=True, color="skyblue", label='Histogram')
            plt.axvline(data_df['diff'].mean(), color='green', linestyle='--',
                        label=f'Mean: {data_df["diff"].mean():.2f}')
            # Draw vertical lines for the standard deviation

            plt.axvline(data_df['diff'].mean() - data_df['diff'].std(), color='purple', linestyle='--',
                        label=f'-1 Std Dev: {(data_df["diff"].mean() - data_df["diff"].std()):.2f}')
            plt.axvline(data_df['diff'].mean() + data_df['diff'].std(), color='purple', linestyle='--',
                        label=f'+1 Std Dev: {(data_df["diff"].mean() + data_df["diff"].std()):.2f}')

            plt.text(data_df['diff'].mean() - data_df['diff'].std(), plt.ylim()[1] * 0.95,
                        f'-1 SD: {data_df["diff"].std():.2f}', horizontalalignment='right',
                        color='purple')
            plt.text(data_df['diff'].mean() + data_df['diff'].std(), plt.ylim()[1] * 0.95,
                        f'+1 SD: {data_df["diff"].std():.2f}', horizontalalignment='left',
                        color='purple')

            plt.title(f'Distribution of Diff Values for {unique_id} ({platform_name})')
            plt.xlabel('Diff')
            plt.ylabel('Frequency')
            plt.legend()
            plt.savefig(output_plot_path)
            plt.close()
            plots_made += 1
    
    return files_extracted, plots_made

def _detect_outliers(data: gpd.GeoDataFrame, scaler: StandardScaler, threshold_percentile:
                     float, original_gdf: gpd.GeoDataFrame, return_smoothed: bool = False) -> tuple[pd.Series | gpd.GeoDataFrame, int]:
    # Normalize data
    data_scaled = scaler.fit_transform(data)

    # Use MICE algorithm for Predictive Mean Matching Imputation
    imputer = IterativeImputer(
        estimator=LinearRegression(),
        max_iter=15,
        random_state=42,
        sample_posterior=False
    )
    
    # Apply imputation
    data_imputed = imputer.fit_transform(data_scaled)
    
    # Smooth the imputed depth values
    smoothed_depth = uniform_filter1d(data_imputed[:, 2], size=50)
    
    # Calculate residuals and detect outliers
    residuals = np.abs(data_scaled[:, 2] - smoothed_depth)
    threshold = np.percentile(residuals, threshold_percentile)
    outliers = residuals > threshold
    
    # Denormalize smoothed imputed depth
    assert scaler.scale_
    assert scaler.mean_
    smoothed_depth_denorm = smoothed_depth * scaler.scale_[2] + scaler.mean_[2]
    
    # Update the original GeoDataFrame's `Outlier` column
    original_gdf.loc[data.index[outliers], 'Outlier'] = True

    # Count the number of outliers
    outlier_count: int = np.sum(outliers)

    if return_smoothed:
        # Create a full-length smoothed depth array, filling with NaN for removed rows
        full_smoothed_depth = pd.Series(index=original_gdf.index, dtype=float)
        full_smoothed_depth[data.index] = smoothed_depth_denorm
        return full_smoothed_depth, outlier_count

    # Remove outliers from the current dataset for the next iteration
    return data[~outliers], outlier_count

def plot_outlier_analysis(gdf: gpd.GeoDataFrame, op_filename: Path, **kwargs) -> None:
    verbose: bool = kwargs.get('verbose', False)
    title: str = kwargs.get('plot_title', 'Plot title not set')
    with_smoothed_depths: bool = kwargs.get('with_smoothed', False)
    
    _, ax = plt.subplots(figsize=(12, 6))
    ax.scatter(
        gdf[~gdf['Outlier']].index, gdf[~gdf['Outlier']]['depth'],
        color='blue', s=1, label="Valid"
    )
    ax.scatter(
        gdf[gdf['Outlier']].index, gdf[gdf['Outlier']]['depth'],
        color='red', s=10, label="Outlier"
    )
    if with_smoothed_depths:
        ax.plot(
            gdf.index, gdf['Final_Smoothed_Depth'],
            color='green', linewidth=1.5, label="Final Smoothed Depth"
        )
    ax.set_title(title)
    ax.set_xlabel("Index")
    ax.set_ylabel("Depth")
    ax.legend()
    plt.savefig(op_filename)
    plt.close()
    if verbose:
        print(f"Saved plot to {op_filename}")

def outlier_detect_df(gdf: pd.DataFrame | gpd.GeoDataFrame, **kwargs) -> pd.Series:
    verbose: bool = kwargs.get('verbose', False)
    gdf['Outlier'] = False
    processed_data = gdf[["lat", "lon", "depth"]]
    scaler = StandardScaler()
    # Pass 1: Lenient threshold (99th percentile)
    if verbose:
        print("[blue]Debug:[/] First Pass (Lenient Threshold - 99th percentile):")
    filtered_data_1, outlier_count_1 = _detect_outliers(
        processed_data.copy(),
        scaler,
        threshold_percentile=99,
        original_gdf=gdf
    )
    assert isinstance(filtered_data_1, gpd.GeoDataFrame) or isinstance(filtered_data_1, pd.DataFrame)
    if verbose:
        print(f"[blue]Debug:[/] Outliers detected in Pass 1: {outlier_count_1}")

    # Pass 2: Moderate threshold (98th percentile)
    if verbose:
        print("[blue]Debug:[/] Second Pass (Moderate Threshold - 98th percentile):")
    filtered_data_2, outlier_count_2 = _detect_outliers(
        filtered_data_1.copy(),
        scaler,
        threshold_percentile=98,
        original_gdf=gdf
    )
    assert isinstance(filtered_data_2, gpd.GeoDataFrame) or isinstance(filtered_data_1, pd.DataFrame)
    if verbose:
        print(f"[blue]Debug:[/] Outliers detected in Pass 2: {outlier_count_2}")

    # Pass 3: Final threshold (98th percentile, return smoothed depth)
    if verbose:
        print("[blue]Debug:[/] Third Pass (Strict Threshold - 98th percentile):")
    final_smoothed_depth, outlier_count_3 = _detect_outliers(
        filtered_data_2.copy(),
        scaler,
        threshold_percentile=98,
        original_gdf=gdf,
        return_smoothed=True
    )
    if verbose:
        print(f"[blue]Debug:[/] Outliers detected in Pass 3: {outlier_count_3}")
    return final_smoothed_depth

def outlier_detect_gpkg(filename: Path, output: Path, **kwargs) -> bool:
    verbose: bool = kwargs.get('verbose', False)
    plot_dir: str = kwargs.get('plot_dir', '')
    if verbose:
            print(f'[blue]Debug:[/] Processing {filename} ...')
        
    if not filename.exists():
        raise ValueError(f'nominal GeoPackage file |{filename}| does not exist')
    if not filename.is_file():
        raise ValueError(f'nominal GeoPackage file |{filename}| is not a file')
    
    try:
        gdf: gpd.GeoDataFrame = gpd.read_file(filename)
    except Exception as e:
        raise ValueError(f'error reading |{filename}| as a GeoPackage')
    if not {'lat', 'lon', 'depth'}.issubset(gdf.columns):
        if verbose:
            print(f"[red]Error:[/] Skipping {filename}: Missing required columns.")
        return False
    try:
        final_smoothed_depth = outlier_detect_df(gdf, **kwargs)

        # Save the processed GeoDataFrame to a new GeoPackage
        gdf.to_file(output, driver='GPKG')
        if verbose:
            print(f"[blue]Debug:[/] Saved processed file to {output}")

        if plot_dir:
            # Assign the final smoothed depth back to the GeoDataFrame
            gdf['Final_Smoothed_Depth'] = final_smoothed_depth
            op_filename: Path = Path(plot_dir) / filename.with_suffix('.png').name
            plot_outlier_analysis(gdf, op_filename, plot_title=f"Final Outlier Detection for {op_filename}",
                                  with_smoothed=True, **kwargs)

    except Exception as e:
        print(f"[red]Error:[/] Failed processing {filename}: {e}")

    return True

def create_transit_ids(df: pd.DataFrame, max_hours_gap: float = 4.0, max_days_duration: float = 7.0) -> pd.DataFrame:
    df = df.sort_values(by='time')
    current_transit_id = None
    last_time = None
    current_start_time = None
    transit_ids = []
    for _, row in df.iterrows():
        if last_time is None or (row['time'] - last_time > timedelta(hours=max_hours_gap)) \
           or ((row['time'] - current_start_time) > timedelta(days=max_days_duration)):
            current_transit_id = f"{row['unique_id']}_{row['time'].strftime('%Y-%m-%d_%H-%M-%S')}"
            current_start_time = row['time']
        transit_ids.append(current_transit_id)
        last_time = row['time']
    df['transit_id'] = transit_ids
    return df

def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Calculate the great-circle distance between two points on the Earth.
    Parameters (lat1, lon1, lat2, lon2) are in decimal degrees.
    Returns the distance in meters.
    """
    R = 6371000.0  # Earth's radius in meters
    phi1, phi2 = np.radians(lat1), np.radians(lat2)
    dphi = np.radians(lat2 - lat1)
    dlambda = np.radians(lon2 - lon1)
    a = np.sin(dphi / 2)**2 + np.cos(phi1) * np.cos(phi2) * np.sin(dlambda / 2)**2
    c = 2 * np.arctan2(np.sqrt(a), np.sqrt(1 - a))
    return R * c

def calculate_vessel_speed(group: pd.DataFrame) -> pd.DataFrame:
    """
    Given a DataFrame group (with at least 'time', 'lat', and 'lon' columns),
    calculates the raw vessel speed (m/s) between consecutive points and
    applies a uniform smoothing filter. Returns the modified group with two
    new columns: 'vessel_speed' and 'vessel_speed_smoothed'.
    """
    # Ensure the DataFrame is sorted by time and work on a copy
    group = group.sort_values('time').copy()
    
    # Ensure latitude and longitude are numeric to avoid NoneType issues.
    group['lat'] = pd.to_numeric(group['lat'], errors='coerce')
    group['lon'] = pd.to_numeric(group['lon'], errors='coerce')
    
    # Calculate time differences (in seconds)
    group['time_diff'] = group['time'].diff().dt.total_seconds()
    
    # Calculate distances (in meters) between consecutive points using haversine
    group['distance'] = _haversine(
        group['lat'].shift(), group['lon'].shift(),
        group['lat'], group['lon']
    )
    
    # Compute raw vessel speed in m/s; first record will have NaN, so fill with 0
    group['vessel_speed'] = group['distance'] / group['time_diff']
    group['vessel_speed'] = group['vessel_speed'].fillna(0)
    
    # Apply a uniform smoothing filter to the vessel speed; adjust window size as needed
    group['vessel_speed_smoothed'] = uniform_filter1d(group['vessel_speed'], size=5)
    
    return group

def make_transits_by_id(con: duckdb.DuckDBPyConnection, unique_id: str, output_dir: Path,
                        maxgap: float, maxduration: float, **kwargs) -> None:
    verbose: bool = kwargs.get('verbose', False)
    df = transit_df(con, unique_id)
    df = create_transit_ids(df, maxgap, maxduration)
    for transit_id, group in df.groupby('transit_id'):
        if verbose:
            print(f"\n[blue]Debug:[/] Processing unique_id {unique_id}, transit {transit_id}")
        group['Outlier'] = False
        data_for_outlier = group[['lat', 'lon', 'depth']].copy()
        group['Final_Smoothed_Depth'] = outlier_detect_df(data_for_outlier, verbose=verbose)
        group = calculate_vessel_speed(group)
        group['Outlier'] = group['Outlier'].astype(bool)

        if 'plots_dir' in kwargs:
            plot_filename = Path(kwargs['plots_dir']) / f'{unique_id}_{transit_id}_outlier_plot.png'
            plot_outlier_analysis(group, plot_filename,
                                  title=f"Outlier Detection for Transit {transit_id} (unique_id: {unique_id})",
                                  **kwargs)
            
        update_db_for_transits(con, group)
        gdf = gpd.GeoDataFrame(group, geometry=gpd.points_from_xy(group.lon, group.lat))
        gdf.set_crs(epsg=4326, inplace=True)
        gdf.to_crs(epsg=26903, inplace=True)  # Adjust EPSG as needed
        start_date = group['time'].min().strftime('%Y%m%d%H%M%S')
        end_date = group['time'].max().strftime('%Y%m%d%H%M%S')
        gpkg_filename: Path = output_dir / f"{unique_id}_{start_date}_{end_date}.gpkg"
        gdf.to_file(gpkg_filename, driver='GPKG')
        if verbose:
            print(f"[blue]Debug:[/] Exported GeoPackage {gpkg_filename}")

        if 'geotiff_dir' in kwargs:
            non_outlier_gdf = gdf[gdf['Outlier'] == False]
            if not non_outlier_gdf.empty:
                tiff_filename = gpkg_filename.with_suffix('.tif')
                create_geotiff(non_outlier_gdf, tiff_filename, kwargs['geotiff_res'])
                if verbose:
                    print(f"[blue]Debug:[/] Exported GeoTIFF {tiff_filename}")
            else:
                if verbose:
                    print(f"[orange]Warning:[/] No non-outlier points in transit {transit_id} to export as GeoTIFF.")
