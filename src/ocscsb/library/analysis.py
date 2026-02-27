from pathlib import Path
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
    
    _, ax = plt.subplots(figsize=(12, 6))
    ax.scatter(
        gdf[~gdf['Outlier']].index, gdf[~gdf['Outlier']]['depth'],
        color='blue', s=1, label="Valid"
    )
    ax.scatter(
        gdf[gdf['Outlier']].index, gdf[gdf['Outlier']]['depth'],
        color='red', s=10, label="Outlier"
    )
    ax.plot(
        gdf.index, gdf['Final_Smoothed_Depth'],
        color='green', linewidth=1.5, label="Final Smoothed Depth"
    )
    ax.set_title(f"Final Outlier Detection for {op_filename}")
    ax.set_xlabel("Index")
    ax.set_ylabel("Depth")
    ax.legend()
    plt.savefig(op_filename)
    plt.close()
    if verbose:
        print(f"Saved plot to {op_filename}")

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
        
    gdf['Outlier'] = False
    processed_data = gdf[["lat", "lon", "depth"]]
    scaler = StandardScaler()
    try:
        # Pass 1: Lenient threshold (99th percentile)
        if verbose:
            print("[blue]Debug:[/] First Pass (Lenient Threshold - 99th percentile):")
        filtered_data_1, outlier_count_1 = _detect_outliers(
            processed_data.copy(),
            scaler,
            threshold_percentile=99,
            original_gdf=gdf
        )
        assert isinstance(filtered_data_1, gpd.GeoDataFrame)
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
        assert isinstance(filtered_data_2, gpd.GeoDataFrame)
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

        # Save the processed GeoDataFrame to a new GeoPackage
        gdf.to_file(output, driver='GPKG')
        if verbose:
            print(f"[blue]Debug:[/] Saved processed file to {output}")

        if plot_dir:
            # Assign the final smoothed depth back to the GeoDataFrame
            gdf['Final_Smoothed_Depth'] = final_smoothed_depth
            plot_outlier_analysis(gdf, Path(plot_dir) / filename.with_suffix('.png').name, **kwargs)

    except Exception as e:
        print(f"[red]Error:[/] Failed processing {filename}: {e}")

    return True
