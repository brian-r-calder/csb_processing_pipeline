# Crowdsourced Bathymetry (CSB) Processing Pipeline

This application provides a full end-to-end workflow for processing raw Crowdsourced Bathymetry (CSB) data into analysis-ready geospatial products. It features a graphical user interface (GUI) to manage inputs, processing steps, and outputs, making complex geospatial operations accessible.

The pipeline ingests raw CSV files, applies tidal and vertical offset corrections, performs statistical outlier detection, and generates final products such as a consolidated points GeoPackage and gridded GeoTIFF rasters.

## Features

- **Tide Correction:** Applies tidal adjustments to depth soundings using data from the NOAA Tides and Currents API and a user-provided tide zone shapefile. location of supplied zoned tide model polygons shapefile:
C:\Pydro24\NOAA\site-packages\Python3\svn_repo\HSTB\CSB_processing\BETA_subordinate_tide_zones\tide_zone_polygons_new_WGS84_merge.shp
- **Vertical Offset Calibration:** Compares CSB data against a reference bathymetric surface to calculate and apply a vertical offset for each vessel, normalizing data from different sources.
- **Flexible Reference Surfaces:**
    - **Automated BlueTopo Download:** Automatically fetches the latest NOAA BlueTopo bathymetric tiles for the specific area covered by the input CSB data.
    - **Local File Support:** Allows the user to provide their own local reference bathymetry in BAG or GeoTIFF format.
- **Advanced Outlier Detection:** Employs a multi-pass statistical routine using `scikit-learn`'s `IterativeImputer` to flag potential outliers in each vessel's transit.
- **Database Backend:** Utilizes DuckDB for efficient intermediate data storage, allowing for complex and fast queries on the processed data.
- **Incremental Processing & Append-Only Workflow:**
    - The pipeline is designed for continuous data integration. If an existing csb.duckdb database and master_offsets.csv file are found in the output directory, the script will use them as a baseline.
    - New CSB data is processed and appended to the existing database.
    - It leverages existing vessel offsets from master_offsets.csv to avoid re-calculating them, improving consistency and speed.
    - Post-processing steps are applied only to the newly added data.
- **Flexible Final Products:**
    - **Intermediate Products (Optional):** Per-transit GeoPackages and plots.
    - **Final Products (Optional):**
        - A consolidated points GeoPackage (`csb_final_points.gpkg`) in EPSG:4326.
        - A gridded GeoTIFF (`csb_final_gridded.tif`) created by averaging non-outlier points.
- **Tiled Processing:** Supports providing a tessellation shapefile to process large areas into smaller, tiled GeoTIFFs and GeoPackages.
- **VRT Creation (Optional):** Automatically organizes tiled GeoTIFFs by their coordinate system and builds a VRT (Virtual Raster) with overviews for each group, making them easy to use in GIS software.
- **GUI:** A user-friendly graphical interface built with Tkinter to manage all inputs and processing options.
- **Cleanup:** Automatically removes temporary files after processing.

## How to Install

The code should be installed in a Python virtual environment, either directly or through conda.  However, there are dependencies that can only really be reliably installed through conda, making it the recommended solution.  To do this, change to the source directory and then:
```shell
conda env create -f environment.yml
conda activate ocscsb
```

The default installation generates four commands:

1. `ocscsb`  This is a utility command with a number of sub-commands used primarily to manipulate the state of the system, and generally only useful by expert users.  The one exception to this is `ocscsb scrape` which is used to download CSV data from the NCEI DCDB point store for use in processing:
```
% ocscsb scrape --help
Usage: ocscsb scrape [OPTIONS] INPUT_SHP OUTPUT_DIR EMAIL [START_DATE]

  Search the DCDB archive API for CSB files from AWS.

  This command queries the DCDB point-store API on AWS to find the CSV
  versions of the contributed CSB files for a given tile of data, as specified
  in the input Shapefile INPUT_SHP.  The CSVs retrieved are stored in
  OUTPUT_DIR.  The EMAIL specified is used for the API ordering information,
  and data is filtered to be after START_DATE (default: 1970-01-01).

Options:
  --help  Show this message and exit.
  ```

2. `processing`.  This launches a simple GUI that lets you specify the location of the various components of a processing run [as detailed below](#how-to-use).

3. `leaderboard`.  This is a small utility that reads the processing state for a particular output directory and generates some summary statistics suitable for use with `dashboard`.

4. `dasboard`.  A simple Dash-driven web interface to display the run statistics and histograms.

## How to Use

1.  **Prepare Your Input Data:**
    - **Raw CSB Directory:** A folder containing one or more CSB data files in `.csv` format.
    - **Tide Zone Shapefile:** A shapefile that defines the geographic zones for tide correction. It must contain the columns `ControlStn`, `ATCorr`, and `RR`.
    - **Output Directory:** An empty folder where all output products will be saved.
    - **(Optional) Reference Bathymetry:** A local BAG or GeoTIFF file if you are not using the automated BlueTopo download.
    - **(Optional) Tessellation Shapefile:** A polygon shapefile to use for tiled processing.

2.  **Launch the Application:**

    The `processing` command starts the GUI, from within the prepared environment:
    ```
    conda activate ocscsb
    processing
    ```

3.  **Configure the Processing Run via the GUI:**

    - **Section 1: Input Files & Folders:** Fill in the paths to your CSB data, tide zone file, and output directory.
    - **Section 2: Reference Bathymetry:**
        - Check "Use Automated BlueTopo Download" to have the script fetch reference data automatically.
        - OR, uncheck it and provide the path to your own local BAG or GeoTIFF file.
    - **Section 3: Processing & Export Options:**
        - **Insert into DuckDB:** (Recommended: ON) This is required for all post-processing and final gridding steps.
        - **Run Post-Processing:** (Recommended: ON) Enables the vertical offset calculation, uncertainty assignment, and outlier flagging. The "Final Gridding" option depends on this step.
        - **Export Individual Transit Files:** (Optional) If you want to inspect the results of the outlier flagging for every single transit, check this box. Be aware that this can create a very large number of files.
        - **Run Final Gridding & Export:** (Recommended: ON) Enables the final, primary output stage.
        - **Export Final Points GeoPackage:** (Recommended: ON) Creates the final `csb_final_points.gpkg` file.
        - **Optional Tessellation Shapefile:** Provide a shapefile here to enable tiled processing mode. If left blank, the script will produce a single set of final products.
        - **Grid Resolution (meters):** Set the cell size for the final output GeoTIFF.
        - **Organize GeoTIFFs ... and Create VRTs:** (Recommended: ON for tiled mode) Automatically sorts tiled outputs and builds virtual rasters.

4.  **Start Processing:** Click the "Start Processing" button. The application window will close automatically when the entire process is complete. Monitor the console for progress updates.

## Output Products

All outputs will be located in the specified **Output Directory**.

- `csb.duckdb`: The intermediate DuckDB database file containing all processed data.
- `master_offsets.csv`: A CSV file containing the calculated vertical offsets for each vessel.
- `/histograms/`: A folder containing PNG histograms showing the distribution of offsets for each vessel.
- `/transit_exports/`: (If enabled) A folder containing plots and GeoPackages for each individual vessel transit.
- `/final_products/`: This folder contains the primary deliverables.
    - `csb_final_points.gpkg`: (If enabled) A GeoPackage containing all processed points in EPSG:4326, with an `outlier` field indicating their status.
    - `csb_final_gridded.tif` or `<id>_gridded.tif`: The final gridded raster(s) in the appropriate UTM zone(s).
    - `/EPSG_.../`: (If VRT creation is enabled) Subfolders where the GeoTIFFs are organized by their coordinate system. Each folder will contain a `mosaic_EPSG_....vrt` file, which is the best file to load into GIS software.


***Contact Anthony Klemm anthony.r.klemm@noaa.gov for any questions regarding this tool*** 



Images: Comparison of CSB grid (IDW algorithm) vs Hydrographic Survey H13387 in Houston, TX Harbor

![Screenshot](https://github.com/anthonyklemm/Crowdsourced_Bathy_Processing/blob/main/csb_vs_BAG.gif?raw=true)
![Screenshot](https://github.com/anthonyklemm/Crowdsourced_Bathy_Processing/blob/main/images/2022-09-29_9-10-00.png?raw=true)
![Screenshot](https://github.com/anthonyklemm/Crowdsourced_Bathy_Processing/blob/main/False_Pass.gif?raw=true)
![Screenshot](https://github.com/anthonyklemm/Crowdsourced_Bathy_Processing/blob/main/images/2022-10-05_22-33-19.png?raw=true)
![Screenshot](https://github.com/anthonyklemm/Crowdsourced_Bathy_Processing/blob/main/images/2022-09-29_8-46-32.png?raw=true)
![Screenshot](https://github.com/anthonyklemm/Crowdsourced_Bathy_Processing/blob/main/images/2022-09-29_8-47-30.png?raw=true)
![Screenshot](https://github.com/anthonyklemm/Crowdsourced_Bathy_Processing/blob/main/images/2022-09-29_16-13-34.png?raw=true)
![Screenshot](https://github.com/anthonyklemm/Crowdsourced_Bathy_Processing/blob/main/images/2022-09-29_9-20-11.png?raw=true)
![Screenshot](https://github.com/anthonyklemm/Crowdsourced_Bathy_Processing/blob/main/images/2022-09-29_9-13-34.png?raw=true)
![Screenshot](https://github.com/anthonyklemm/Crowdsourced_Bathy_Processing/blob/main/images/2022-09-29_16-18-02.png?raw=true)



CSB data pipeline for scraping, tide correction, spatial database population (duckdb-spatial), vertical offset analysis/correction, uncertainty estimation, vessel speed estimation and PMM imputation-based outlier detection algorithms, and exporting data as vessel-transit geopackages and geotiff DEMs. 

There are other helper scripts, and some scripts for dashboard/leaderboard creation as well. 

You're going to need a shapefile of the CO-OPS discrete zoned tide model. One is provided in the OCS Pydro distribution, along with the main CSB processing script. 

Preferred method is to use the BlueTopo bathymetry as the reference bathy, which will be downloaded automatically based on the input raw data coverage, but it also allows the user to use a BAG instead (I think only SR BAGs are supported at this time, but VR BAGs will be supported soon). 

## Development
```shell
conda env create -f environment-dev.yml
```

## Data

### Download FES2022 tidal constituent data
First, register for an Aviso account 
[here](https://www.aviso.altimetry.fr/en/data/products/auxiliary-products/global-tide-fes/release-fes22.html).

Once you have an Aviso account, login using your account to the following SFTP site:

sftp://ftp-access.aviso.altimetry.fr:2221/auxiliary/tide_model

Then download the contents of the `fes2022b/ocean_tide_extrapolated` directory. Once downloaded, unzip each
NetCDF file. Save the .nc files to the directory `data/fes2022b/ocean_tide_extrapolated`.



### Convert NOAA tide polygons to SQLite format
```shell
ogr2ogr -of SQLite -lco 'LAUNDER=NO' tide_zone_polygons.sqlite  tide_zone_polygons.shp
```
