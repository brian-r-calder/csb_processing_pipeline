from pathlib import Path
import duckdb
import hashlib
import pandas as pd
import geopandas as gpd
from rich import print

def ingest_geopackages(source_dir: Path, db_file: Path) -> None:
    if not source_dir.exists() or not source_dir.is_dir():
        raise ValueError(f'|{source_dir}| is not a directory')
    
    table_created: bool = False

    with duckdb.connect(database=db_file.as_posix()) as con:
        con.install_extension('spatial')
        con.load_extension('spatial')
        for filename in source_dir.glob('*.gpkg'):
            print(f'[blue]Info:[/] loading {filename} into DuckDB table')
            if not table_created:
                # Create the table with the first geopackage
                con.execute(f"""
                    CREATE TABLE IF NOT EXISTS csb AS 
                    SELECT * FROM ST_Read('{filename}')
                """)
                table_created = True
            else:
                # Insert subsequent geopackages into the existing table
                con.execute(f"""
                    INSERT INTO csb
                    SELECT * FROM ST_Read('{filename}')
                """)
            print(f'[blue]Info:[/] {filename} loaded successfully')

def replace_depth_diff(db_file: Path, **kwargs) -> None:
    verbose: bool = False
    if 'verbose' in kwargs:
        verbose = kwargs['verbose']

    with duckdb.connect(database=db_file.as_posix()) as con:
        columns_query = "DESCRIBE csb"
        columns_df = con.execute(columns_query).fetchdf()
        if 'diff' in columns_df['column_name'].values:
            if verbose:
                print("[blue]Info:[/] Dropping incorrect 'diff' column in DuckDB...")
            con.execute("ALTER TABLE csb DROP COLUMN diff")

        # Check if the 'diff' column exists and create it with the correct calculation
        columns_df = con.execute(columns_query).fetchdf()
        if 'diff' not in columns_df['column_name'].values:
            if verbose:
                print("[blue]Info:[/] Creating 'diff' column in DuckDB with correct calculation...")
            con.execute("ALTER TABLE csb ADD COLUMN diff DOUBLE DEFAULT NULL")
            con.execute("UPDATE csb SET diff = (depth_new *-1 - Raster_Value)*-1 WHERE diff IS NULL")

def apply_offsets(db_file: Path, **kwargs) -> None:
    verbose: bool = False
    if 'verbose' in kwargs:
        verbose = kwargs['verbose']
    
    with duckdb.connect(database=db_file.as_posix()) as con:
        # Drop the incorrect 'depth_mod' column if it exists
        columns_query = "DESCRIBE csb"
        columns_df = con.execute(columns_query).fetchdf()
        if 'depth_mod' in columns_df['column_name'].values:
            if verbose:
                print("[blue]Info:[/] Dropping incorrect 'depth_mod' column in DuckDB...")
            con.execute("ALTER TABLE csb DROP COLUMN depth_mod")
        if 'uncertainty_vert' in columns_df['column_name'].values:
            if verbose:
                print("[blue]Info:[/] Dropping old 'uncertainty_vert' column in DuckDB...")
            con.execute("ALTER TABLE csb DROP COLUMN uncertainty_vert")
        if 'uncertainty_hori' in columns_df['column_name'].values:
            if verbose:
                print("[blue]Info:[/] Dropping old 'uncertainty_hori' column in DuckDB...")
            con.execute("ALTER TABLE csb DROP COLUMN uncertainty_hori")
        
        # Replace the depth_mod column, offset by the average difference, and filled with the
        # computed final depth if the offset correction is zero.
        con.execute("ALTER TABLE csb ADD COLUMN depth_mod DOUBLE;")
        con.execute("""
        UPDATE csb
        SET depth_mod = (depth_new - sub.average_diff) * -1
        FROM (
            SELECT unique_id, AVG(diff) AS average_diff
            FROM csb
            WHERE diff > -12 AND diff < 12 AND Raster_Value > -20 AND Uncertainty_Value < 3
            GROUP BY unique_id
        ) AS sub
        WHERE csb.unique_id = sub.unique_id;
        """)
        update_query = """
        UPDATE csb
        SET depth_mod = depthfinal
        WHERE depth_mod = 0;
        """
        con.execute(update_query)
        if verbose:
            print("[blue]Info:[/] Updated depth_mod values where they were zero with depthfinal.")

        # The uncertainty model is basically CATZOC C, given prior experience on the quality of the
        # data against reference depths; this intentionally ignores any evidence from the data, giving
        # a worst-case (but safe) assessment.
        uncert_vert_query = """
        UPDATE csb
        SET uncertainty_vert = (2 + (depth_mod * -0.05))
        """
        con.execute("ALTER TABLE csb ADD COLUMN uncertainty_vert DOUBLE;")
        con.execute(uncert_vert_query)

        uncert_hori_query="""
        UPDATE csb
        SET uncertainty_hori = 10
        """
        con.execute("ALTER TABLE csb ADD COLUMN uncertainty_hori DOUBLE;")
        con.execute(uncert_hori_query)
        if verbose:
            print("[blue]Info:[/] Uncertainty values calculated to CATZOC C")

# Function to create synthetic key
def _key(timestamp: int, lat: float, lon: float) -> str:
    composite_string = f"{timestamp}_{lat:.6f}_{lon:.6f}"
    return hashlib.md5(composite_string.encode()).hexdigest()

# Function to process a single GeoPackage and extract outlier synthetic keys
def _make_keys(geo_package_path: Path, **kwargs) -> list[str]:
    verbose: bool = False
    if 'verbose' in kwargs:
        verbose = kwargs['verbose']
    try:
        gdf = gpd.read_file(geo_package_path)
        gdf_outliers = gdf[gdf['Outlier'] == True].copy()
        gdf_outliers['time'] = pd.to_datetime(gdf_outliers['time']).astype('int64') // 10**9
        gdf_outliers['lat'] = gdf_outliers['lat'].astype(float)
        gdf_outliers['lon'] = gdf_outliers['lon'].astype(float)
        gdf_outliers['depth'] = gdf_outliers['depth'].astype(float)

        gdf_outliers['synthetic_key'] = gdf_outliers.apply(
            lambda row: _key(row['time'], row['lat'], row['lon']), axis=1
        )
        return gdf_outliers['synthetic_key'].tolist()
    except Exception as e:
        if verbose:
            print(f"[red]Error:[/] Failed processing GeoPackage {geo_package_path}: {e}")
        return []

def gpkg_outliers_to_db(gpkg_dir: Path, db_file: Path, **kwargs) -> None:
    verbose: bool = False
    if 'verbose' in kwargs:
        verbose = kwargs['verbose']
    
    with duckdb.connect(database=db_file.as_posix()) as con:
        try:
            add_outlier_column = """
            ALTER TABLE csb ADD COLUMN Outlier BOOLEAN DEFAULT FALSE;
            """
            con.execute(add_outlier_column)
        except Exception:
            if verbose:
                print('[orange]Warning:[/] outlier column already exists in db')
        
        # Extract primary outlier data from the DuckDB database so that a "primary" key hash can
        # be constructed that's distinctive for the observation (and therefore should be identifiable
        # in the GeoPackages).
        if verbose:
            print("blue]Info:[/] Fetching data from DuckDB...")
        duckdb_query = """
        SELECT time AS timestamp, lat AS latitude, lon AS longitude, depth_mod AS depth, Outlier
        FROM csb
        """
        duckdb_df = con.execute(duckdb_query).fetchdf()
        if verbose:
            print("blue]Info:[/] Standardizing DuckDB data formats...")
        duckdb_df['timestamp'] = pd.to_datetime(duckdb_df['timestamp']).astype('int64') // 10**9
        duckdb_df['latitude'] = duckdb_df['latitude'].astype(float)
        duckdb_df['longitude'] = duckdb_df['longitude'].astype(float)
        duckdb_df['depth'] = duckdb_df['depth'].astype(float)
        duckdb_df['synthetic_key'] = duckdb_df.apply(
            lambda row: _key(row['timestamp'], row['latitude'], row['longitude']), axis=1
        )
        if verbose:
            print("blue]Debug:[/] Sample synthetic keys from DuckDB:", duckdb_df['synthetic_key'].head().tolist())
        
        # Loop over all of the GeoPackages available, extracting outlier keys, and generating a
        # unique subset thought a set composition
        outlier_keys: set[str] = set()
        for geopackage in gpkg_dir.glob('*.gpkg'):
            if verbose:
                print(f'[blue]Info:[/] Processing GeoPackage {geopackage} ...')
            file_keys = _make_keys(geopackage, verbose=verbose)
            outlier_keys.update(file_keys)
        if verbose:
            print(f'[blue]Debug:[/] Sample synthetic keys from GeoPackages: {outlier_keys[:5]}')
            key_count = duckdb_df['synthetic_key'].isin(outlier_keys).sum()
            print(f'[blue]Info:[/] Total {key_count} matching keys in csb table')
        
        if verbose:
            print(f'[blue]Info:[/] Updating DuckDB Outlier column ...')
        duckdb_df['Outlier'] = duckdb_df['synthetic_key'].isin(outlier_keys)

        # TODO: Determine whether this debugging output is useful in any circumstances
        if verbose:
            print(duckdb_df)
            column_names = duckdb_df.columns
            print(column_names)
            print(duckdb_df.head)
        
        # The processed data needs to be written back to the database, but we want it to be in
        # a separate table to preserve the original data.
        con.execute("DROP TABLE IF EXISTS csb_updated;")
        con.execute("""
        CREATE TABLE csb_updated AS 
        SELECT 
            timestamp, latitude AS lat, longitude AS lon, depth, Outlier
        FROM duckdb_df
        """)
        if verbose:
            print("[blue]Info:[/] Updated Outlier column written back to DuckDB.")
