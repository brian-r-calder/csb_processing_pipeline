from pathlib import Path

from ocscsb.command import scrape

if __name__ == '__main__':
    content_root = Path(__file__).parent.parent.parent.parent
    data_root = content_root / 'data'
    input_shp = data_root / 'tessellation_testing' / 'tessellation_testing.gpkg'
    output_dir = data_root / 'out-dbg'
    email = 'bmiles@ccom.unh.edu'
    scrape([str(input_shp), str(output_dir), email])
