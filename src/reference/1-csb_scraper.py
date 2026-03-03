# -*- coding: utf-8 -*-
"""
CSB Scraper with Server-Side Date Filtering
Updated based on finding undocumented API parameters.
"""

import geopandas as gpd
import requests
import time
import os

# --- CONFIGURATION ---
# Filter for data collected ON or AFTER this date.
# Format must be YYYY-MM-DD
START_DATE = "2025-03-20" 
# ---------------------

def ensure_grid_id_exists(gdf):
    if 'GRID_ID' not in gdf.columns:
        print("GRID_ID field does not exist. Creating and populating it with integers.")
        gdf['GRID_ID'] = ["csb_raw_" + str(i) for i in range(1, len(gdf) + 1)]
    else:
        print("GRID_ID field exists.")
    return gdf

def process_tile(bbox, email, tile_name, output_directory):
    print(f"Processing GRID_ID {tile_name} with bbox: {bbox}")
    
    # --- UPDATED PAYLOAD WITH UNDOCUMENTED DATE FILTER ---
    payload = {
        "email": email,
        "bbox": bbox,
        "datasets": [
            {
                "label": "csb",
                "archive_date": {
                    "start": START_DATE
                }
            }
        ]
    }
    # -----------------------------------------------------

    try:
        response = requests.post('https://q81rej0j12.execute-api.us-east-1.amazonaws.com/order', json=payload)
    except Exception as e:
        print(f"Network error submitting order for {tile_name}: {e}")
        return

    if response.status_code == 201:
        order_response = response.json()
        status_url = order_response.get('url', '')
        
        print(f"Order submitted. Status URL: {status_url}")
        time.sleep(5) # Short pause to let server register
        
        # Wait for the server to prepare the file
        wait_and_download(status_url, tile_name, output_directory)
    else:
        print(f"Failed to create order for GRID_ID {tile_name}. Status: {response.status_code}")
        print(f"Response: {response.text}")

def wait_and_download(status_url, tile_name, output_directory):
    retry_count = 0
    max_retries = 40  # ~10 minutes max wait
    
    while retry_count < max_retries:
        status, output_location = check_order_status(status_url)
        
        if status == 'complete':
            print(f"Order ready! Downloading GRID_ID {tile_name}...")
            
            # Fix URL: Convert s3:// protocol to https://
            filename = output_location.split('/')[-1]
            download_url = f'https://order-pickup.s3.amazonaws.com/{filename}'
            
            local_path = os.path.join(output_directory, f'{tile_name}.csv')
            
            if download_csv(download_url, local_path):
                print(f"Successfully saved {tile_name}.csv")
            return

        elif status == 'error':
            print(f"API Error processing GRID_ID {tile_name}")
            return
            
        # Wait 15s before checking again
        time.sleep(15)
        retry_count += 1
    
    print(f"Timeout: Order for {tile_name} took too long.")

def check_order_status(order_url):
    try:
        r = requests.get(order_url, timeout=10)
        if r.status_code == 200:
            data = r.json()
            return data.get('status', 'processing').lower(), data.get('output_location')
    except Exception:
        pass
    return 'processing', None

def download_csv(url, local_path):
    try:
        r = requests.get(url, stream=True, timeout=60)
        if r.status_code == 200:
            with open(local_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=8192):
                    f.write(chunk)
            return True
        else:
            print(f"Download failed with status {r.status_code}")
    except Exception as e:
        print(f"Download error: {e}")
    return False

def csv_file_exists(tile_name, output_directory):
    return os.path.exists(os.path.join(output_directory, f"{tile_name}.csv"))

def main(input_polygon_path, output_directory, email):
    if not os.path.exists(output_directory):
        os.makedirs(output_directory)

    gdf = gpd.read_file(input_polygon_path).to_crs(epsg=4326)
    gdf = ensure_grid_id_exists(gdf)

    for index, row in gdf.iterrows():
        tile_name = row['GRID_ID']
        
        if csv_file_exists(tile_name, output_directory):
            print(f"Skipping {tile_name} (File exists)")
            continue
        
        bounds = row['geometry'].bounds
        bbox = f"{bounds[0]},{bounds[1]},{bounds[2]},{bounds[3]}"
        
        process_tile(bbox, email, tile_name, output_directory)

if __name__ == "__main__":
    # USER INPUTS
    input_polygon_path = r"C:\Users\Anthony.R.Klemm\Desktop\processing_scripts\tesselation_13Feb2025_1.shp"
    output_directory = r"D:\CSB_raw_start_20MAR25_end08JAN26"
    email = '' # Add your email here - or an empty string if you don't want email notifications
    
    main(input_polygon_path, output_directory, email)