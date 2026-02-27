from pathlib import Path
import seaborn as sns
import matplotlib.pyplot as plt
import duckdb
import pandas as pd
from rich import print


def generate_offset_histograms(db_file: Path, export_dir: Path, **kwargs) -> tuple[int,int]:
    verbose: bool = False
    if 'verbose' in kwargs:
        verbose = kwargs['verbose']

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