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
    # Assert final products exist where expected...
    final_products: io.StorageLocation = output_location_s3['location'].sub_location('final_products')
    assert final_products.contains('CX-12_points.gpkg')
    epsg_32619: io.StorageLocation = final_products.sub_location('EPSG_32619')
    epsg_32619.contains('CX-12_gridded.tif')
    epsg_32619.contains('mosaic_EPSG_32619.vrt')
    epsg_32619.contains('mosaic_EPSG_32619.vrt.ovr')
    histograms: io.StorageLocation = output_location_s3['location'].sub_location('histograms')
    histo_files = histograms.list_files()
    assert len(histo_files) == 24
    transit_exports: io.StorageLocation = output_location_s3['location'].sub_location('transit_exports')
    transit_files = transit_exports.list_files()
    assert len(transit_files) == 32
    assert output_location_s3['location'].contains('VESSEL_OFFSETS_csb_corr_csb_raw_1.csv')
