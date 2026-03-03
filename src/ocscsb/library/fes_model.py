# fes_model.py
import numpy as np
import pyfes
import os
import xarray as xr

def get_fes_tide(lons, lats, times, fes_data_path, template_yaml_path):
    """Calculates FES tides relative to MSL."""
    os.environ['DATASET_DIR'] = fes_data_path
    handlers = pyfes.load_config(template_yaml_path)
    lons_360 = np.mod(lons, 360)
    tide, lp, _ = pyfes.evaluate_tide(handlers['tide'], times, lons_360, lats)
    pure_tide_m = (tide + lp) / 100.0
    return pure_tide_m

def get_lat_separation(lons, lats, fes_data_path):
    """
    Calculates the MSL-to-LAT separation using the fast 'big four' constituent formula.
    This version uses robust nearest-neighbor selection.
    """
    print("Calculating fast LAT separation using M2+S2+K1+O1 formula...")
    constituents_to_load = ['m2', 's2', 'k1', 'o1']
    lons_360 = np.mod(lons, 360)
    
    # Create xarray DataArrays for the coordinates, which is required for .sel
    lons_da = xr.DataArray(lons_360, dims="points")
    lats_da = xr.DataArray(lats, dims="points")
    
    total_amplitude = 0.0 # Initialize as a float

    for const in constituents_to_load:
        filepath = os.path.join(fes_data_path, f"{const}_fes2022.nc")
        with xr.open_dataset(filepath) as ds:
            # Use .sel with method='nearest' to find the value at the closest grid point
            selected_points = ds['amplitude'].sel(lon=lons_da, lat=lats_da, method='nearest')
            total_amplitude += selected_points.to_numpy()
    
    # Separation is the sum of amplitudes, converted from cm to m.
    # It's a positive value representing the distance from MSL down to LAT.
    separation_meters = total_amplitude / 100.0
    separation_meters[np.isnan(separation_meters)] = 0.0
    
    print("Separation calculation complete.")
    return separation_meters