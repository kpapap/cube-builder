#
# This file is part of Cube Builder.
# Copyright (C) 2022 INPE.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <https://www.gnu.org/licenses/gpl-3.0.html>.
#

"""Define the unittests for cube_builder.utils.image module."""

from tempfile import NamedTemporaryFile, TemporaryDirectory

import numpy
import rasterio
from rasterio.crs import CRS
from rasterio.warp import Affine

from cube_builder.utils import image

RASTER_OPTIONS = dict(
    width=100,
    height=100,
    crs=CRS.from_epsg(4326),
    driver='GTiff',
    count=1,
    dtype='int16',
    geotransform=Affine(29.999900761648355, 0.0, 4962528.761448536, 0.0,
                        -29.99952920102161, 9217407.901105449)
)

RASTER_DATA = numpy.random.randint(100, size=(RASTER_OPTIONS['width'], RASTER_OPTIONS['height'])).astype(numpy.int16)


def test_check_file_integrity():
    """Test the file integrity checking."""
    temp_file = NamedTemporaryFile()
    with rasterio.open(temp_file.name, 'w', **RASTER_OPTIONS) as ds:
        ds.write(RASTER_DATA, 1)

    assert image.check_file_integrity(temp_file.name)
    assert not image.check_file_integrity('/tmp/not-exists.tif')


def test_create_empty_raster():
    """Test the creation of empty data cube item."""
    with TemporaryDirectory() as tmp:
        tmp_file = tmp + '/test.tif'
        xmin, ymax = 6138927.355567569, 10645561.92311954
        resolution = [10, 10]
        dist = [168060.048009797, 109861.84106387943]
        nodata = -9999
        proj4 = '+proj=aea +lat_0=-12 +lon_0=-54 +lat_1=-2 +lat_2=-22 +x_0=5000000 +y_0=10000000 +ellps=GRS80 +units=m +no_defs '
        image.create_empty_raster(tmp_file, proj4, dtype='int16', xmin=xmin, ymax=ymax,
                                  resolution=resolution, dist=dist, nodata=nodata)

        with rasterio.open(tmp_file) as ds:
            data = ds.read(1)

            assert data.min() == data.max() == nodata


def test_rescale_raster():
    """Test scale values of array into a new range.

    This test consists in adapt the values of collections like
    Landsat Collection 2 Level 2 transforming
    the range 8000~40000 with additive -0.2 into range 0-10000."""
    # Landsat C2 L2 values
    arr2d = numpy.array([
        [17834, 16269, 8275, 27369],
        [15755, 19081, 21684, 20865],
        [24655, 21416, 8932, 32578],
        [27168, 21827, 11382, 37001],
    ], dtype=numpy.uint16)

    res = image.rescale(arr2d, 0.0000275, new_scale=0.0001, origin_additive=-0.2)

    assert res.max() < 10000
    expected = numpy.array([[2904, 2473,  275, 5526],
                            [2332, 3247, 3963, 3737],
                            [4780, 3889,  456, 6958],
                            [5471, 4002, 1130, 8175]], dtype=numpy.uint16)
    assert numpy.array_equal(res, expected)


def test_linear_raster_scale():
    """Test linear raster scaling to transform band values int16 into byte."""
    arr2d = numpy.random.randint(0, 10000, (10, 10), dtype=numpy.uint16)

    # Cast to uint8
    rescaled_arr2d = image.linear_raster_scale(arr2d, (0, arr2d.max(),), (0, 255))

    assert 0 <= rescaled_arr2d.min() <= rescaled_arr2d.max()

    # Using masked array
    arr2d_ma = numpy.ma.array(arr2d, mask=11000)
    rescaled_arr2d_ma = image.linear_raster_scale(arr2d_ma, (0, arr2d_ma.max(),), (0, 255))
    assert 0 <= rescaled_arr2d.min() <= rescaled_arr2d.max()


def test_bit_extraction():
    """Test bit extraction from any value (used for Landsat sensors)."""
    # 43 => 0010 1011
    for position in [0, 1, 3, 5]:
        bit_pos_value = image.extract_qa_bits(43, position)
        expected = 2 ** position
        assert bit_pos_value == expected


def test_radsat_bit():
    data = numpy.array([1026, 1037], dtype=numpy.uint16)
    saturated_values = image.radsat_extract_bits(data, 1, 7)
    expected = numpy.array([1, 6], dtype=numpy.uint16)
    assert numpy.array_equal(saturated_values, expected)
