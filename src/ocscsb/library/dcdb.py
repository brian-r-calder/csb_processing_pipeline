import time
from pathlib import Path

import geopandas as gpd

import requests

from rich import print

from ocscsb.library import io


def ensure_grid_id_exists(gdf: gpd.GeoDataFrame) -> gpd.GeoDataFrame:
    if 'GRID_ID' not in gdf.columns:
        print("[orange]Warning:[/] GRID_ID field does not exist. Creating and populating it with integers.")
        gdf['GRID_ID'] = ["csb_raw_" + str(i) for i in range(1, len(gdf) + 1)]
    return gdf

def check_order_status(order_url: str, max_tries: int = 10) -> tuple[str,str|None]:
    print(f"[blue]Info:[/] Checking status at: {order_url}")

    for attempt in range(max_tries):
        response = requests.get(order_url)
        print(f"[blue]Info:[/] Attempt {attempt + 1}, HTTP status code: {response.status_code}")

        if response.status_code == 200:
            try:
                status_response = response.json()
                status = status_response.get('status', 'error')
                output_location = status_response.get('output_location')
                print(f"[blue]Info:[/] Order status: {status}, Output location: {output_location}")
                return status, output_location
            except ValueError:
                print("[red]Error:[/] Failed parsing JSON response:", response.text)
                return 'error', None
        else:
            print(f"[orange]Warning:[/] Failed to check order status. HTTP status code: {response.status_code}. Retrying in 15 seconds...")
            time.sleep(15)  # Wait 15 seconds before retrying

    # If all ten attempts fail, return an error status
    print(f"[red]Error:[/] Failed to retrieve a valid order status after {max_tries} attempts.")
    return 'error', None


# Download CSV from the provided URL
def download_csv(url: str, csv_file: io.File) -> bool:
    try:
        response = requests.get(url, stream=True, timeout=60)
        if response.status_code == 200:
            with csv_file.open(mode='wb') as file:
                for chunk in response.iter_content(chunk_size=8192):
                    file.write(chunk)
            print(f"[blue]Info:[/] CSV file has been downloaded to {csv_file.get_uri()}")
            return True
        else:
            print(f"[red]Error:[/] Failed to download CSV. HTTP status code: {response.status_code}")
    except Exception as e:
        print(f'[red]Error:[/] Failed downloading {url}: {e}')
    return False

def process_tile(bbox: str, email: str, start_date: str, tile_name: str, storage_location: io.StorageLocation, **kwargs) -> None:
    print(f"[blue]Info:[/] Processing GRID_ID {tile_name} with bbox: {bbox}")
    payload = {
        "email": email,
        "bbox": bbox,
        "datasets": [
            {
                "label": "csb",
                "archive_date": {
                    "start": start_date
                }
            }
        ]
    }

    # Submit the order
    if 'url' in kwargs:
        url: str = kwargs['url']
    else:
        url: str = 'https://q81rej0j12.execute-api.us-east-1.amazonaws.com/order'

    print(f'[blue]Info:[/] sending request to API at {url}')
    try:
        response = requests.post(url, json=payload)
    except Exception as e:
        raise RuntimeError(f'[red]Error:[/] Network error submitting order for {tile_name}: {e}')
    
    if response.status_code == 201:
        order_response = response.json()

        # Use the 'url' field directly from the response for status checking
        status_url = order_response.get('url', '')
        print(f"[blue]Info:[/] Using status URL: {status_url}")  # Debug print to verify the status URL

        # Introduce a delay before checking the status for the first time
        time.sleep(5)
    else:
        raise RuntimeError(f"[red]Error:[/] Failed to create order for GRID_ID {tile_name}:", response.text)

    # Wait for order completion; check order status and download CSV
    retry_count = 0
    max_retries = 40 # ~ 10 min max wait
    
    while retry_count < max_retries:
        status, output_location = check_order_status(status_url)
        if status == 'complete':
            print(f"[blue]Info:[/] Order completed for GRID_ID {tile_name}. Download data from: {output_location}")
            assert isinstance(output_location, str)
            filename = output_location.split('/')[-1]
            download_url: str = f'https://order-pickup.s3.amazonaws.com/{filename}'
            csv_file: io.File = storage_location.new_file(f"{tile_name}.csv")
            if download_csv(download_url, csv_file):
                print(f"[blue]Info:[/] CSV file for GRID_ID {tile_name} processing can start now.")
            return
        elif status == 'error':
            print(f"[red]Error:[/] Failed in processing the order for GRID_ID {tile_name}.")
            break
        
        print(f"[blue]Info:[/] Order for GRID_ID {tile_name} is still processing. Waiting... (Attempt {retry_count + 1}/{max_retries})")
        time.sleep(15)
        retry_count += 1

    if retry_count == max_retries:
        print(f"[red]Error:[/] Order for GRID_ID {tile_name} did not complete after {max_retries} attempts.")
