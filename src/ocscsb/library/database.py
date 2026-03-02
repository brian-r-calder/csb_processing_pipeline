from pathlib import Path
import duckdb
import hashlib
import pandas as pd
import geopandas as gpd
from shapely.geometry import Point
from rich import print

def enable_spatial(con: duckdb.DuckDBPyConnection) -> None:
    con.install_extension('spatial')
    con.load_extension('spatial')

def db_unique_ids(con: duckdb.DuckDBPyConnection) -> list[str]:
    db_ids = con.execute('SELECT DISTINCT unique_id FROM csb;').fetchall()
    return [str(x[0]) for x in db_ids]

def ingest_geopackages(source_dir: Path, db_file: Path) -> None:
    if not source_dir.exists() or not source_dir.is_dir():
        raise ValueError(f'|{source_dir}| is not a directory')
    
    table_created: bool = False

    with duckdb.connect(database=db_file.as_posix()) as con:
        enable_spatial(con)
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

def apply_depth_offsets(db_file: Path, **kwargs) -> None:
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
            print(f'[blue]Debug:[/] Sample synthetic keys from GeoPackages: {list(outlier_keys)[:5]}')
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

def _add_column(con: duckdb.DuckDBPyConnection, name: str, type: str, **kwargs) -> bool:
    verbose: bool = kwargs.get('verbose', False)
    try:
        con.execute(f"ALTER TABLE csb ADD COLUMN {name} {type};")
        if verbose:
            print(f"[blue]Debug:[/] Added {name} column to csb.")
    except Exception as e:
        if verbose:
            print("[orange]Warning:[/] {name}} column already exists or could not be added:", e)
        return False
    return True

def augment_db_for_transits(con: duckdb.DuckDBPyConnection, **kwargs) -> bool:
    verbose: bool = kwargs.get('verbose', False)
    rc: bool = _add_column(con, 'synthetic_key', 'VARCHAR', **kwargs)
    rc |= _add_column(con, 'Outlier', 'BOOLEAN DEFAULT FALSE')
    
    update_synthetic_key_query = """
    UPDATE csb
    SET synthetic_key = md5(
        cast(EXTRACT(epoch FROM STRPTIME(time, '%Y%m%d %H:%M:%S')) as varchar)
        || '_' || printf('%.6f', CAST(lat as DOUBLE))
        || '_' || printf('%.6f', CAST(lon as DOUBLE))
    )
    """
    con.execute(update_synthetic_key_query)
    if verbose:
        print("[blue]Debug:[/] Updated synthetic_key values in csb.")
    
    rc |= _add_column(con, 'transid_id', 'VARCHAR')
    rc |= _add_column(con, 'vessel_speed_smoothed', 'DOUBLE')

    return rc

def transit_df(con: duckdb.DuckDBPyConnection, unique_id: str) -> pd.DataFrame:
    query = f"""
        SELECT unique_id, platform_name_x AS platform_name, time, depth_mod AS depth,
               uncertainty_vert AS uncertainty, uncertainty_hori, lat, lon, synthetic_key
        FROM csb
        WHERE unique_id = '{unique_id}'
        AND depth_mod IS NOT NULL
        ORDER BY time;
        """
    df = con.execute(query).df()
    # Convert time column to datetime
    df['time'] = pd.to_datetime(df['time'], format='%Y%m%d %H:%M:%S')

    return df

def update_db_for_transits(con: duckdb.DuckDBPyConnection, df: pd.DataFrame) -> None:
    updates = []
    for _, row in df.iterrows():
        updates.append((row['synthetic_key'], row['transit_id'], str(row['Outlier']).upper()))
    if updates:
        values_clause = ", ".join(
            f"('{sk}', '{tid}', {outlier})" for sk, tid, outlier in updates
        )
        bulk_update_query = f"""
        UPDATE csb
        SET transit_id = t.transit_id, Outlier = t.outlier
        FROM (VALUES {values_clause}) AS t(synthetic_key, transit_id, outlier)
        WHERE csb.synthetic_key = t.synthetic_key;
        """
        con.execute(bulk_update_query)
        print("Batch updated csb table for Outlier and transit_id for this transit group.")

    speed_updates = []
    for _, row in df.iterrows():
        speed_updates.append((row['synthetic_key'], row['vessel_speed_smoothed']))
    if speed_updates:
        values_clause = ", ".join(
            f"('{sk}', {speed})" for sk, speed in speed_updates
        )
        update_speed_query = f"""
        UPDATE csb
        SET vessel_speed_smoothed = t.speed
        FROM (VALUES {values_clause}) AS t(synthetic_key, speed)
        WHERE csb.synthetic_key = t.synthetic_key;
        """
        con.execute(update_speed_query)
        print("Batch updated vessel speed values in csb table for this transit group.")

def export_db_to_gpkg(db_file: Path, gpkg_file: Path, **kwargs) -> gpd.GeoDataFrame:
    verbose = kwargs.get('verbose', False)
    epsg = kwargs.get('epsg', 4326)
    shapefile = kwargs.get('shapefile', False)
    with duckdb.connect(database=db_file) as con:
        enable_spatial(con)
        df = con.execute("""SELECT * FROM csb WHERE Outlier = 0 AND depth_mod IS NOT NULL""").df()

        # If there is a 'geom' column (with WKB data), drop it so we can build our geometry from lat and lon.
        if 'geom' in df.columns:
            df = df.drop(columns=['geom'])

        gdf = gpd.GeoDataFrame(df, geometry=gpd.points_from_xy(df.lon, df.lat))
        gdf.set_crs(epsg=4326, inplace=True)
        gdf.to_crs(epsg=epsg, inplace=True)

        gpkg_file.parent.mkdir(parents=True, exist_ok=True)
        if shapefile:
            gdf.to_file(gpkg_file)
        else:
            gdf.to_file(gpkg_file, driver='GPKG')
        if verbose:
            print(f'[blue]Debug:[/] GeoPackage successfully written to {gpkg_file}.')
    return gdf

def query_by_bbox(con: duckdb.DuckDBPyConnection, bbox: dict[str,float]) -> gpd.GeoDataFrame:
    """
    Queries the DuckDB CSB table for points within the
    provided bounding box (in WGS84) that are not flagged as outliers.
    Assumes the table contains 'lon', 'lat', 'depth_mod', and 'Outlier' columns.
    """
    query = f"""
    SELECT *
    FROM csb
    WHERE CAST(lon AS DOUBLE) BETWEEN {bbox['min_lon']} AND {bbox['max_lon']}
      AND CAST(lat AS DOUBLE) BETWEEN {bbox['min_lat']} AND {bbox['max_lat']}
      AND depth_mod IS NOT NULL
      AND Outlier IS FALSE
    """
    df = con.execute(query).fetchdf()
    geometry = [Point(xy) for xy in zip(df.lon, df.lat)]
    gdf = gpd.GeoDataFrame(df, geometry=geometry, crs="EPSG:4326")
    return gdf
