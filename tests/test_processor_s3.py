from pathlib import Path

import pytest

from .fixtures import (s3_client, garage_credentials, garage_layout, data_path, csb_path,
                       csb_location_s3, output_location_s3, fes_data_path, tessellation_path)

from ocscsb.library.processing import Processor
from ocscsb.library import io

def test_csb_s3_path(csb_s3_location):
    assert csb_s3_location.contains('csb_raw_1-trunc.csv')

def test_processor_s3(s3_client, csb_location_s3, output_location_s3, fes_data_path, tessellation_path):
    proc: Processor = Processor(
        csb_location_s3['location_str'],
        output_location_s3['location_str'],
        provider='S3',
        provider_args={'client': s3_client['client']},
        use_bluetopo=True,
        insert_duckdb=True,
        run_analysis=True,
        use_fes_model=True,
        fes_data_path=str(fes_data_path),
        run_final_grid=True,
        export_final_gpkg=True,
        tessellation_shp=str(tessellation_path),
        organize_vrt=True
    )
    proc.run()
    # TODO: Assert final products exist where expected...
