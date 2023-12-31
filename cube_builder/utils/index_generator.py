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

"""Simple data cube band generator."""

import logging
from typing import Dict, List

import numpy
from bdc_catalog.models import Band, Collection

from .image import SmartDataSet, generate_cogs
from .interpreter import execute

BandMapFile = Dict[str, str]
"""Type which a key (represented as data cube band name) points to generated file in disk."""


def generate_band_indexes(cube: Collection, scenes: dict, period: str, tile_id: str, reuse_data_cube: Collection = None,
                          **kwargs) -> BandMapFile:
    """Generate data cube custom bands based in string-expression on table `band_indexes`.

    This method seeks for custom bands on Collection Band definition. A custom band must have
    `metadata` property filled out according the ``bdc_catalog.jsonschemas.band-metadata.json``.

    Note:
        When collection does not have any index band, returns empty dict.

    Raises:
        RuntimeError: when an error occurs while interpreting the band expression in Python Virtual Machine.

    Returns:
        A dict values with generated bands.
    """
    from .processing import build_cube_path

    cube_band_indexes: List[Band] = []

    for band in cube.bands:
        if band.metadata_ and band.metadata_.get('expression') and band.metadata_['expression'].get('value'):
            cube_band_indexes.append(band)

    if not cube_band_indexes:
        return dict()

    map_data_set_context = dict()
    profile = None
    blocks = []

    for band_name, file_path in scenes.items():
        map_data_set_context[band_name] = SmartDataSet(str(file_path), mode='r')

        if profile is None:
            profile = map_data_set_context[band_name].dataset.profile.copy()
            blocks = list(map_data_set_context[band_name].dataset.block_windows())

    if not blocks or profile is None:
        raise RuntimeError('Can\t generate band indexes since profile/blocks is None.')

    output = dict()
    cube_name = cube.name
    cube_version = cube.version
    if reuse_data_cube:
        cube_name = reuse_data_cube['name']
        cube_version = reuse_data_cube['version']

    for band_index in cube_band_indexes:
        band_name = band_index.name

        band_expression = band_index.metadata_['expression']['value']

        band_data_type = band_index.data_type

        data_type_info = numpy.iinfo(band_data_type)

        data_type_max_value = data_type_info.max
        data_type_min_value = data_type_info.min

        profile['dtype'] = band_data_type
        profile['nodata'] = float(band_index.nodata)

        custom_band_path = build_cube_path(cube_name, period, tile_id, version=cube_version, band=band_name,
                                           **kwargs)

        output_dataset = SmartDataSet(str(custom_band_path), mode='w', **profile)
        logging.info(f'Generating band {band_name} for cube {cube_name} - {custom_band_path.stem}...')

        for _, window in blocks:
            machine_context = {
                k: ds.dataset.read(1, masked=True, window=window).astype(numpy.float32)
                for k, ds in map_data_set_context.items()
            }

            expr = f'{band_name} = {band_expression}'

            result = execute(expr, context=machine_context)
            raster = result[band_name]

            # Persist the expected band data type to cast value safely.
            # TODO: Should we use consider band min_value/max_value?
            raster[raster < data_type_min_value] = data_type_min_value
            raster[raster > data_type_max_value] = data_type_max_value

            output_dataset.dataset.write(raster.astype(band_data_type), window=window, indexes=1)

        output_dataset.close()

        generate_cogs(str(custom_band_path), str(custom_band_path))

        output[band_name] = str(custom_band_path)

    return output
