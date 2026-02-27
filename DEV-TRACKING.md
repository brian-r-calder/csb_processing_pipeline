# Command Script Conversion

The original functions/scripts that were available in the repository have been moved from the root directory to src/reference, with the example scripts (leaderboards, dashboard, and what looks like some working scripts) have been moved to src/examples.

In order to extract commonality, make a library that's separate from the GUI, and to provide a CLI version as a prelude to containerisation, the numbered scripts are in the process of being converted to library functions in src/ocscsb/library and click-based CLI commands while the GUI (embedded in script 2-csb_processing.py) is being converted into src/ocscsb/gui.

## Functions Completed

The individual Python scripts for manipulations are translated primarily into functions in the library as below, with a click-based CLI defined in command/__init__.py and sub-commands for each of the original files:

* 1-csb_scraper.py -> scraper
    
    library/dcdb:
        
        ensure_grid_id_exists()
        check_order_status()
        download_csv()
        process_tile()
        csv_file_exists()

* 3-load_csb_to_duckdb -> ingest
    
    library/database
        
        ingest_geopackages()

* 4-histograms_and_calibration_points.py -> offset-pmfs
    
    library/analysis
        
        generate_offset_histograms()
    
    library/database
        
        replace_depth_diff()

* 5-apply_best_offsets_duckdb.py -> apply-offsets
    
    library/database
        
        apply_offsets()

* 8-insert_outlier_flags_in_duckdb.py -> outlier-ingest

    library/database

        _key()
        _make_keys()
        gpkg_outliers_to_db()

## Functions To Go

* 2-csb_processing.py
* 6-export_transits_to_gpkg_and_tiff_2.py
* 6-export_transits_to_gpkg_and_tiff_speed.py
* 7-Outlier_model_PMM_Imputation.py
* 9-csb_export_all_points_create_geotiff.py
* 10-csb_differencing_visualizations.py

# Outstanding Questions

1. There are two scripts numbered "6" and they are very similar although different in detail.  Which is the preferred one?

2. Do the numbered scripts get used in seequence, or are they used randomly as required?

3. What combination of scripts is required for a "standard" workflow?

4. In a number of the scripts, columns in the DuckDB database are removed and replaced, and the comments note that the columns are "incorrect" and are being replaced.  It's unclear, however, why the columns didn't use the correct calculations in the first instance!  What's the reasoning here.

5. Validate whether the extensive output (e.g., printing the whole DataFrame for outliers) in gpkg_outliers_to_db() is required in any known use-case.