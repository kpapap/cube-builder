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

"""Define celery tasks utilities for datacube generation."""

# Python Native
import logging
import shutil
import warnings
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import List, Tuple, Union

# 3rdparty
import numpy
import rasterio
import rasterio.features
import rasterio.windows
import requests
from bdc_catalog.models import Collection, Item, SpatialRefSys, Tile, db
from geoalchemy2.shape import from_shape, to_shape
from numpngw import write_png
from rasterio import Affine, MemoryFile
from rasterio.warp import Resampling, reproject
from sqlalchemy import func

from ..config import Config
# Constant to define required bands to generate both NDVI and EVI
from ..constants import (CLEAR_OBSERVATION_ATTRIBUTES, CLEAR_OBSERVATION_NAME, COG_MIME_TYPE, DATASOURCE_ATTRIBUTES,
                         DATASOURCE_NAME, PROVENANCE_ATTRIBUTES, PROVENANCE_NAME, SRID_ALBERS_EQUAL_AREA,
                         TOTAL_OBSERVATION_NAME)
from ..drivers.datasets import dataset_from_uri
# Builder
from . import get_srid_column
from .image import (SmartDataSet, generate_cogs, get_resample_method, linear_raster_scale, raster_convexhull,
                    raster_extent, rescale, save_as_cog)
from .index_generator import generate_band_indexes
from .strings import StringFormatter

FORMATTER = StringFormatter()

Limit = Tuple[int, int]
"""Represent the type for Image range."""
ChannelLimits = Tuple[Limit, Limit, Limit]
"""Represent the band channels (R, G, B) for quick look generation."""


def get_rasterio_config() -> dict:
    """Retrieve cube-builder global config for the rasterio module."""
    options = dict()

    if Config.RASTERIO_ENV and isinstance(Config.RASTERIO_ENV, dict):
        options.update(Config.RASTERIO_ENV)

    return options


def get_or_create_model(model_class, defaults=None, **restrictions):
    """Define a utility method for looking up an object with the given restrictions, creating one if necessary.

    Args:
        model_class (BaseModel) - Base Model of Brazil Data Cube DB
        defaults (dict) - Values to fill out model instance
        restrictions (dict) - Query Restrictions
    Returns:
        BaseModel Retrieves model instance
    """
    instance = db.session.query(model_class).filter_by(**restrictions).first()

    if instance:
        return instance, False

    params = dict((k, v) for k, v in restrictions.items())

    params.update(defaults or {})
    instance = model_class(**params)

    db.session.add(instance)

    return instance, True


def get_or_create_activity(cube: str, warped: str, activity_type: str, scene_type: str, band: str,
                           period: str, tile_id: str, activity_date: str, **parameters):
    """Define a utility method for create activity."""
    return dict(
        band=band,
        collection_id=cube,
        warped_collection_id=warped,
        activity_type=activity_type,
        tags=parameters.get('tags', []),
        status='CREATED',
        date=activity_date,
        period=period,
        scene_type=scene_type,
        args=parameters,
        tile_id=tile_id
    )


def get_item_id(datacube: str, version: int, tile: str, date: str, fmt=None) -> str:
    """Prepare a data cube item structure."""
    if fmt is None:
        fmt = '{datacube:upper}_V{version}_{tile_id}_{start_date}'

    return FORMATTER.format(
        fmt,
        datacube=datacube,
        version=str(version),
        version_legacy='{0:03d}'.format(int(version)),
        tile_id=tile,
        date=date,
        start_date=date.replace('-', '')[:8]
    )


def merge(merge_file: str, mask: dict, assets: List[dict], band: str,
          band_map: dict, quality_band: str, collection: str, build_provenance=False, compute=False,
          native_grid: bool = False, **kwargs):
    """Apply datacube merge scenes.

    The Merge or Warp consists in a procedure that cropping and mosaicking all imagens that superimpose a target tile
    of common grid, for a specific date.

    See also:
        `Warp (Merge, Reprojecting, Resampling and Griding) <https://brazil-data-cube.github.io/products/specifications/processing-flow.html#warp-merge-reprojecting-resampling-and-griding>`_

    Args:
        merge_file: Path to store data cube merge
        assets: List of collections assets during period
        band: Merge band name
        band_map: Map of cube band name and common name.
        build_provenance: Build a provenance file for Merge (Used in combined collections)
        **kwargs: Extra properties
    """
    from .image import QAConfidence
    xmin = kwargs.get('xmin')
    ymax = kwargs.get('ymax')
    dist_x = kwargs.get('dist_x')
    dist_y = kwargs.get('dist_y')
    datasets = kwargs.get('datasets')
    platforms = kwargs.get('platforms', [])
    resx, resy = kwargs['resx'], kwargs['resy']
    block_size = kwargs.get('block_size')
    shape = kwargs.get('shape', None)
    transform = None

    if native_grid:
        tile_id = kwargs['tile_id']
        cname, collection_version = collection.rsplit('-', 1)
        collection = Collection.query().filter(
            Collection.name == cname,
            Collection.version == collection_version
        ).first_or_404(f'Collection {collection} not found')
        geom_table = collection.grs.geom_table
        if geom_table is None:
            raise RuntimeError(f'The Grid {collection.grs.name} not found.')

        srid_column = get_srid_column(geom_table.c)
        query = db.session.query(
            func.ST_SetSRID(geom_table.c.geom, srid_column).label('geom'),
            SpatialRefSys.proj4text.label('crs'),
        ).join(SpatialRefSys, SpatialRefSys.srid == srid_column).filter(geom_table.c.tile == tile_id).first()
        if query is None:
            raise RuntimeError(f'Tile {tile_id} not found')
        geom = to_shape(query.geom)
        xmin, ymin, xmax, ymax = geom.bounds
        transform = Affine(resx, 0, xmin, 0, -resy, ymax)
        cols = int((xmax - xmin) / resx)
        rows = int((ymax - ymin) / resy)
        shape = None

        kwargs['srs'] = query.crs

    elif shape:
        cols = shape[0]
        rows = shape[1]
    else:
        cols = round(dist_x / resx)
        rows = round(dist_y / resy)

        new_res_x = dist_x / cols
        new_res_y = dist_y / rows

        transform = Affine(new_res_x, 0, xmin, 0, -new_res_y, ymax)

    srs = kwargs['srs']

    if isinstance(datasets, str):
        warnings.warn(
            'Parameter "dataset" got str, expected list of str. It will be deprecated in future.'
        )
        datasets = [datasets]

    source_nodata = None
    for _asset in assets:
        source_nodata = _asset['nodata']
        break

    nodata = float(band_map[band]['nodata'])
    if source_nodata is None:
        source_nodata = float(band_map[band]['nodata'])

    data_type = band_map[band]['data_type']
    resampling = Resampling.nearest

    if quality_band == band:
        source_nodata = nodata = float(mask['nodata'])
    elif "resampling" in kwargs:
        resampling = get_resample_method(kwargs["resampling"])

    elif (mask and mask.get('saturated_band') != band) or quality_band is None:
        resampling = Resampling.bilinear

    raster = numpy.zeros((rows, cols,), dtype=data_type)
    raster_merge = numpy.full((rows, cols,), dtype=data_type, fill_value=nodata)
    confidence = None

    if build_provenance:
        raster_provenance = numpy.full((rows, cols,),
                                       dtype=DATASOURCE_ATTRIBUTES['data_type'],
                                       fill_value=DATASOURCE_ATTRIBUTES['nodata'])
        if mask.get('bits') and mask.get('confidence'):
            conf = mask['confidence']
            conf.setdefault('oli', True)
            confidence = QAConfidence(**conf)

    template = None
    is_combined_collection = len(datasets) > 1 or (len(platforms) > 1 and kwargs.get('combined'))
    index_landsat_oli = []
    platforms_used = []

    with rasterio_access_token(kwargs.get('token')) as options:
        with rasterio.Env(CPL_CURL_VERBOSE=False, **get_rasterio_config(), **options):
            for asset in assets:
                link = asset['link']

                dataset = asset['dataset']
                platform = asset.get('platform', '')

                if platform and 'landsat' in platform.lower() and kwargs.get('combined'):
                    _, platform_version = platform.split('-' if '-' in platform else '_')
                    is_oli = int(platform_version) > 7
                    if is_oli:
                        index_landsat_oli.append(platforms.index(platform))

                _check_rio_file_access(link, access_token=kwargs.get('token'))
                if platform:
                    platforms_used.append(platform)

                src = dataset_from_uri(link, band=band, extra_data=asset)

                with src:
                    meta = src.meta.copy()
                    meta.update({
                        'width': cols,
                        'height': rows
                    })
                    if not shape:
                        meta.update({
                            'crs': srs,
                            'transform': transform
                        })

                    if src.profile['nodata'] is not None:
                        source_nodata = src.profile['nodata']
                    elif 'LC8SR' in dataset or 'LC8_SR' in dataset:
                        if band != quality_band:
                            # Temporary workaround for landsat
                            # Sometimes, the laSRC does not generate the data set properly and
                            # the data maybe UInt16 instead Int16
                            source_nodata = nodata if src.profile['dtype'] == 'int16' else 0
                    elif 'CBERS' in dataset and band != quality_band:
                        source_nodata = nodata

                    meta.update({
                        'nodata': source_nodata,
                        'driver': 'GTiff',
                        'count': 1   # Ensure that output data is always single band
                    })

                    with MemoryFile() as mem_file:
                        with mem_file.open(**meta) as dst:
                            if shape:
                                raster = src.read(1)
                            else:
                                source_array = src.read(1)

                                reproject(
                                    source=source_array,
                                    destination=raster,
                                    src_transform=src.transform,
                                    src_crs=src.crs,
                                    dst_transform=transform,
                                    dst_crs=srs,
                                    src_nodata=source_nodata,
                                    dst_nodata=nodata,
                                    resampling=resampling)

                            if kwargs.get('scale') and band != quality_band:
                                new_scale = multiplier = float(band_map[band].get('scale', band_map.get('scale_mult')))
                                additive = float(band_map[band].get('scale_add') or 0)
                                if isinstance(kwargs['scale'], dict):
                                    multiplier = kwargs['scale']['mult']
                                    additive = kwargs['scale'].get('add')

                                raster_ma = numpy.ma.array(raster, dtype=raster.dtype, mask=raster == nodata)
                                raster_ma = rescale(raster_ma, multiplier, new_scale=new_scale, origin_additive=additive)
                                raster = raster_ma.data

                            # For combined collections, we must merge only valid data into final data set
                            if is_combined_collection:
                                positions_todo = numpy.where(raster_merge == nodata)

                                if positions_todo:
                                    valid_positions = numpy.where(raster != nodata)

                                    raster_todo = numpy.ravel_multi_index(positions_todo, raster.shape)
                                    raster_valid = numpy.ravel_multi_index(valid_positions, raster.shape)

                                    # Match stack nodata values with observation
                                    # stack_raster_where_nodata && raster_where_data
                                    intersect_ravel = numpy.intersect1d(raster_todo, raster_valid)

                                    if len(intersect_ravel):
                                        where_intersec = numpy.unravel_index(intersect_ravel, raster.shape)
                                        raster_merge[where_intersec] = raster[where_intersec]

                                        if build_provenance:
                                            # TODO: Improve way to get fixed Value instead. Use GUI mapping?
                                            raster_provenance[where_intersec] = datasets.index(dataset)
                            else:
                                valid_data_scene = raster[raster != nodata]
                                raster_merge[raster != nodata] = valid_data_scene.reshape(numpy.size(valid_data_scene))
                                valid_data_scene = None

                            if template is None:
                                template = dst.profile

                            if build_provenance:
                                raster_masked = numpy.ma.masked_where(raster == nodata, raster)

                                where_valid = numpy.invert(raster_masked.mask)
                                # TODO: Review and validate this step.
                                # Using according to the given collections order.
                                raster_provenance[where_valid] = platforms.index(platform)

                                where_valid = None
                                raster_masked = None

    template['dtype'] = data_type
    template['nodata'] = nodata

    if build_provenance and mask.get('bits') and mask.get('confidence'):
        confidence.oli = numpy.isin(raster_provenance, index_landsat_oli)

    # Evaluate cloud cover and efficacy if band is quality
    efficacy = 0
    cloudratio = 100
    raster = None
    if band == quality_band:
        efficacy, cloudratio = _qa_statistics(raster_merge, mask=mask, compute=compute, confidence=confidence)

    # Ensure file tree is created
    merge_file = Path(merge_file)
    merge_file.parent.mkdir(parents=True, exist_ok=True)

    options = dict(
        file=str(merge_file),
        efficacy=efficacy,
        cloudratio=cloudratio,
        dataset=dataset,
        resolution=resx,
        nodata=nodata,
        platforms_used=list(set(platforms_used))
    )

    if band == quality_band and build_provenance:
        provenance = merge_file.parent / merge_file.name.replace(band, DATASOURCE_NAME)

        profile = deepcopy(template)
        profile['dtype'] = DATASOURCE_ATTRIBUTES['data_type']
        profile['nodata'] = DATASOURCE_ATTRIBUTES['nodata']
        entries = datasets
        if len(datasets) == 1:
            entries = platforms
            options['platforms'] = platforms

        custom_tags = {dataset: value for value, dataset in enumerate(entries)}

        save_as_cog(str(provenance), raster_provenance, tags=custom_tags, block_size=block_size, **profile)
        options[DATASOURCE_NAME] = str(provenance)

    # Persist on file as Cloud Optimized GeoTIFF
    save_as_cog(str(merge_file), raster_merge.astype(data_type), block_size=block_size, **template)

    return options


def _check_rio_file_access(url: str, access_token: str = None):
    """Make a HEAD request in order to check if the given resource is available and reachable."""
    headers = dict()
    if access_token:
        headers.update({'X-Api-Key': access_token})
    try:
        if url and not url.startswith('http'):
            return

        _ = requests.head(url, headers=headers)
    except requests.exceptions.ConnectionError as e:
        raise ConnectionError(f'Connection refused {e.request.url}')
    except requests.exceptions.HTTPError as e:
        if e.response is None:
            raise
        reason = e.response.reason
        msg = str(e)
        if e.response.status_code == 403:
            if e.request.headers.get('x-api-key') or 'access_token=' in e.request.url:
                msg = "You don't have permission to request this resource."
            else:
                msg = 'Missing Authentication Token.'
        elif e.response.status_code == 500:
            msg = 'Could not request this resource.'

        raise requests.exceptions.HTTPError(f'({reason}) {msg}', request=e.request, response=e.response)


def post_processing_quality(quality_file: str, bands: List[str], cube: str,
                            date: str, tile_id, quality_band: str, band_map: dict, version: int,
                            datasets=None, **kwargs):
    """Stack the merge bands in order to apply a filter on the quality band.

    We have faced some issues regarding `nodata` value in spectral bands, which was resulting
    in wrong provenance date on STACK data cubes, since the Fmask tells the pixel is valid (0) but a nodata
    value is found in other bands.
    To avoid that, we read all the others bands, seeking for `nodata` value. When found, we set this to
    nodata in Fmask output::

        Quality             Nir                   Quality

        0 0 2 4      702  876 7000 9000      =>    0 0 2 4
        0 0 0 0      687  987 1022 1029      =>    0 0 0 0
        0 2 2 4    -9999 7100 7322 9564      =>  255 2 2 4

    Args:
         quality_file: Path to the merge fmask.
         bands: All the bands from the merge date.
         cube: Identity data cube name
         date: Merge date
         tile_id: Brazil data cube tile identifier
         quality_band: Quality band name
         version: Data cube version
         datasets: List of related data sets used
    """
    block_size = kwargs.get('block_size')
    # Get quality profile and chunks
    with rasterio.open(str(quality_file)) as merge_dataset:
        blocks = list(merge_dataset.block_windows())
        profile = merge_dataset.profile
        nodata = profile.get('nodata', band_map[quality_band]['nodata'])
        if nodata is not None:
            nodata = float(nodata)
        raster_merge = merge_dataset.read(1)

    _default_bands = DATASOURCE_NAME, 'ndvi', 'evi', 'cnc', TOTAL_OBSERVATION_NAME, CLEAR_OBSERVATION_NAME, PROVENANCE_NAME

    bands_without_quality = [b for b in bands if b != quality_band and b.lower() not in _default_bands]

    saturated = nodata
    for dataset in datasets:
        if dataset is not None and (dataset.lower().startswith('s2') or dataset.lower().startswith('sentinel')):
            saturated = 1
            logging.info('Using saturated value 1 for Sentinel-2...')

    for _, block in blocks:
        nodata_positions = []

        row_offset = block.row_off + block.height
        col_offset = block.col_off + block.width

        nodata_scl = raster_merge[block.row_off: row_offset, block.col_off: col_offset] == nodata

        for band in bands_without_quality:
            band_file = build_cube_path(cube, date, tile_id, version=version, band=band,
                                        prefix=Config.WORK_DIR, composed=False, **kwargs)

            with rasterio.open(str(band_file)) as ds:
                raster = ds.read(1, window=block)

            band_nodata = band_map[band]['nodata']
            nodata_found = numpy.where(raster == float(band_nodata))
            raster_nodata_pos = numpy.ravel_multi_index(nodata_found, raster.shape)
            nodata_positions = numpy.union1d(nodata_positions, raster_nodata_pos)

        if len(nodata_positions):
            raster_merge[block.row_off: row_offset, block.col_off: col_offset][
                numpy.unravel_index(nodata_positions.astype(numpy.int64), raster.shape)] = saturated
            raster_merge[block.row_off: row_offset, block.col_off: col_offset][nodata_scl] = nodata

    save_as_cog(str(quality_file), raster_merge, block_size=block_size, **profile)


def compute_data_set_stats(file_path: str, mask: dict, compute: bool = True) -> Tuple[float, float]:
    """Compute data set efficacy and cloud ratio.

    It opens the given ``file_path`` and calculate the mask statistics, such efficacy and cloud ratio.

    Args:
        file_path - Path to given data set
        data_set_name - Data set name (LC8SR, S2SR_SEN28, CBERS, etc)

    Returns:
        Tuple consisting in efficacy and cloud ratio, respectively.
    """
    with rasterio.open(file_path, 'r') as data_set:
        raster = data_set.read(1)

        efficacy, cloud_ratio = _qa_statistics(raster, mask=mask, compute=compute)

    return efficacy, cloud_ratio


def blend(activity, band_map, quality_band, build_clear_observation=False, block_size=None,
          reuse_data_cube=None, apply_valid_range=None, **kwargs):
    """Apply blend and generate raster from activity.

    Basically, the blend operation consists in stack all the images (merges) in period. The stack is based in
    best pixel image (Best clear ratio). The cloud pixels are masked with `numpy.ma` module, enabling to apply
    temporal composite function MEDIAN, AVG over these rasters.

    The following example represents a data cube Landsat-8 16 days using function Best Pixel (Stack - LCF) and
    Median (MED) in period of 16 days from 1/1 to 16/1. The images from `10/1` and `15/1` were found and the values as
    described below::

        10/1
        Quality                Nir

        0 0 2 4         702  876 7000 9000
        0 1 1 4         687  444  421 9113      =>  Clear Ratio = 50%
        0 2 2 4        1241 1548 2111 1987      =>  Cloud Ratio = 50%

        15/1
        Quality           Nir
        0 0 255 255     854 756 9800 9454
        0 1   1   1     945 400  402  422       =>  Clear Ratio ~= 83%
        0 0   0   0     869 975  788  799       =>  Cloud Ratio ~= 0%

    According to Brazil Data Cube User Guide, the best image is 15/1 (clear ratio ~83%) and worst as 10/1 (50%).
    The result data cube will be::

        Landsat-8_30_16D_LCF
        Quality        Nir                     Provenance (Day of Year)

        0 0 2 4       854 756 7000 9000      15 15 10 10
        0 1 1 1       945 400  411  422      15 15 15 15
        0 0 0 0       869 975  788  799      15 15 15 15

        Landsat-8_30_16D_MED
        Nir

        778  816 -9999 -9999
        816  422   402   422
        1055 975   788   799

    Note:
        When build_clear_observation is set, make sure to do not execute in parallel processing
        since it is not `thread-safe`.
        The provenance band is not generated by MEDIAN products.
        For pixels `nodata` in the best image, the cube builder will try to find useful pixel in the next observation.
        It may be cloud/cloud-shadow (when there is no valid pixel 0 and 1). Otherwise, fill as `nodata`.

    See Also:
        - `Numpy Masked Arrays <https://numpy.org/doc/stable/reference/maskedarray.generic.html>`_

        - `Brazil Data Cube Temporal Compositing <https://brazil-data-cube.github.io/products/specifications/processing-flow.html#temporal-compositing>`_

    Args:
        activity: Prepared blend activity metadata
        band_map: Map of data cube bands (common_name : name)
        build_clear_observation: Flag to dispatch generation of Clear Observation band. It is not ``thread-safe``.

    Returns:
        A processed activity with the generated values.
    """
    from .image import QAConfidence, get_qa_mask, radsat_extract_bits

    # Assume that it contains a band and quality band
    numscenes = len(activity['scenes'])

    band = activity['band']
    activity_mask = activity['mask']
    mask_values = None

    version = activity['version']

    nodata = activity.get('nodata', band_map[band]['nodata'])
    if band == quality_band:
        nodata = activity_mask['nodata']

    # Get basic information (profile) of input files
    keys = list(activity['scenes'].keys())

    filename = activity['scenes'][keys[0]]['ARDfiles'][band]

    with rasterio.open(filename) as src:
        profile = src.profile
        tilelist = list(src.block_windows())

    platforms = activity.get('platforms', [])
    is_combined_collection = len(activity['datasets']) > 1 or (len(platforms) > 1 and kwargs.get('combined'))
    index_landsat_oli = []
    for idx, platform in enumerate(platforms):
        if 'landsat' in platform.lower():
            _, platform_version = platform.split('-')
            if int(platform_version) >= 8:
                index_landsat_oli.append(idx)
    # Order scenes based in efficacy/resolution
    mask_tuples = []

    for key in activity['scenes']:
        scene = activity['scenes'][key]
        resolution = scene.get('resx') or scene.get('resy') or scene.get('resolution')

        efficacy = scene['efficacy']
        resolution = resolution
        mask_tuples.append((100. * efficacy / resolution, key))

    # Open all input files and save the datasets in two lists, one for masks and other for the current band.
    # The list will be ordered by efficacy/resolution
    masklist = []
    bandlist = []

    provenance_merge_map = dict()
    merges_band_map = {}

    for m in sorted(mask_tuples, reverse=True):
        key = m[1]
        efficacy = m[0]
        scene = activity['scenes'][key]

        filename = scene['ARDfiles'][quality_band]
        quality_ref = rasterio.open(filename)

        if mask_values is None:
            mask_values = parse_mask(activity_mask)

        try:
            masklist.append(quality_ref)
        except BaseException as e:
            raise IOError('FileError while opening {} - {}'.format(filename, e))

        filename = scene['ARDfiles'][band]
        merges_band_map[filename] = key

        provenance_merge_map.setdefault(key, None)

        if scene['ARDfiles'].get(DATASOURCE_NAME):
            provenance_merge_map[key] = SmartDataSet(scene['ARDfiles'][DATASOURCE_NAME])

        try:
            bandlist.append(rasterio.open(filename))
        except BaseException as e:
            raise IOError('FileError while opening {} - {}'.format(filename, e))

    # Build the raster to store the output images.
    width = profile['width']
    height = profile['height']
    min_value, max_value = float(band_map[band]['min_value']), float(band_map[band]['max_value'])

    # Get the map values
    clear_values = mask_values['clear_data']
    not_clear_values = mask_values['not_clear_data']
    saturated_list = []
    if mask_values.get('saturated_band'):
        for m in sorted(mask_tuples, reverse=True):
            key = m[1]
            scene = activity['scenes'][key]

            filename = scene['ARDfiles'][mask_values['saturated_band']]
            saturated_file = SmartDataSet(filename, mode='r')
            saturated_list.append(saturated_file)

    saturated_values = mask_values['saturated_data']

    # STACK will be generated in memory
    stack_raster = numpy.full((height, width), dtype=profile['dtype'], fill_value=nodata)
    # Build the stack total observation
    stack_total_observation = numpy.zeros((height, width), dtype=numpy.uint8)

    datacube = activity.get('datacube')
    period = activity.get('period')
    tile_id = activity.get('tile_id')

    if reuse_data_cube:
        datacube = reuse_data_cube['name']
        version = reuse_data_cube['version']

    cube_file = build_cube_path(datacube, period, tile_id, version=version, band=band, suffix='.tif',
                                composed=True, **kwargs)

    # Create directory
    cube_file.parent.mkdir(parents=True, exist_ok=True)

    cube_function = activity['composite_function']

    confidence = None
    if mask_values['bits']:
        conf = mask_values.get('confidence', dict())
        conf.setdefault('oli', True)
        confidence = QAConfidence(**conf)

    if cube_function == 'MED':
        median_raster = numpy.full((height, width), fill_value=nodata, dtype=profile['dtype'])

    if build_clear_observation:
        logging.info('Creating and computing Clear Observation (ClearOb) file...')

        clear_ob_file_path = build_cube_path(datacube, period, tile_id, version=version,
                                             band=CLEAR_OBSERVATION_NAME, suffix='.tif', composed=True, **kwargs)
        dataset_file_path = build_cube_path(datacube, period, tile_id, version=version,
                                            band=DATASOURCE_NAME, suffix='.tif', composed=True, **kwargs)

        clear_ob_profile = profile.copy()
        clear_ob_profile['dtype'] = CLEAR_OBSERVATION_ATTRIBUTES['data_type']
        clear_ob_profile.pop('nodata', None)
        clear_ob_data_set = SmartDataSet(str(clear_ob_file_path), 'w', **clear_ob_profile)

        dataset_profile = profile.copy()
        dataset_profile['dtype'] = DATASOURCE_ATTRIBUTES['data_type']
        dataset_profile['nodata'] = DATASOURCE_ATTRIBUTES['nodata']

        if is_combined_collection:
            datasets = activity['datasets']
            entities = datasets
            if kwargs.get('combined'):
                entities = platforms
            tags = {dataset: value for value, dataset in enumerate(entities)}

            datasource = SmartDataSet(str(dataset_file_path), 'w', tags=tags, **dataset_profile)
            datasource.dataset.write(numpy.full((height, width),
                                     fill_value=DATASOURCE_ATTRIBUTES['nodata'],
                                     dtype=DATASOURCE_ATTRIBUTES['data_type']), indexes=1)

    provenance_array = numpy.full((height, width), dtype=numpy.int16, fill_value=-1)

    for _, window in tilelist:
        # Build the stack to store all images as a masked array. At this stage the array will contain the masked data
        stackMA = numpy.ma.zeros((numscenes, window.height, window.width), dtype=numpy.int16)

        notdonemask = numpy.ones(shape=(window.height, window.width), dtype=numpy.bool_)

        if build_clear_observation and is_combined_collection:
            data_set_block = numpy.full((window.height, window.width),
                                        fill_value=DATASOURCE_ATTRIBUTES['nodata'],
                                        dtype=DATASOURCE_ATTRIBUTES['data_type'])

        row_offset = window.row_off + window.height
        col_offset = window.col_off + window.width

        # For all pair (quality,band) scenes
        for order in range(numscenes):
            # Read both chunk of Merge and Quality, respectively.
            ssrc = bandlist[order]
            msrc = masklist[order]
            raster = ssrc.read(1, window=window)
            masked = msrc.read(1, window=window, masked=True)
            copy_mask = numpy.array(masked, copy=True)

            if saturated_list:
                saturated = saturated_list[order].dataset.read(1, window=window)
                # TODO: Get the original band order and apply to the extract function instead.
                saturated = radsat_extract_bits(saturated, 1, 7).astype(numpy.bool_)
                masked.mask[saturated] = True

            # Get current observation file name
            file_path = bandlist[order].name
            file_date = datetime.strptime(merges_band_map[file_path], '%Y-%m-%d')
            day_of_year = file_date.timetuple().tm_yday

            if build_clear_observation and is_combined_collection:
                datasource_block = provenance_merge_map[file_date.strftime('%Y-%m-%d')].dataset.read(1, window=window)
                if mask_values['bits']:
                    confidence.oli = numpy.isin(datasource_block, index_landsat_oli)

            if mask_values['bits']:
                matched = get_qa_mask(masked,
                                      clear_data=clear_values,
                                      not_clear_data=not_clear_values,
                                      nodata=mask_values['nodata'],
                                      confidence=confidence)  # TODO: Pass the QA Confidence
                masked.mask = matched.mask
            else:
                # Mask cloud/snow/shadow/no-data as False
                masked.mask[numpy.where(numpy.isin(masked, not_clear_values))] = True
                # Ensure that Raster no data value (-9999 maybe) is set to False
                masked.mask[raster == nodata] = True
                masked.mask[numpy.where(numpy.isin(masked, saturated_values))] = True
                # Mask valid data (0 and 1) as True
                masked.mask[numpy.where(numpy.isin(masked, clear_values))] = False

            # Create an inverse mask value in order to pass to numpy masked array
            # True => nodata
            bmask = masked.mask

            # Use the mask to mark the fill (0) and cloudy (2) pixels
            stackMA[order] = numpy.ma.masked_where(bmask, raster)

            # Copy Masked values in order to stack total observation
            # Use numpy where before to locate positions to change
            # mask all and then apply the valid data over copy mask count
            valid_pos = numpy.where(copy_mask != nodata)
            copy_mask[copy_mask == nodata] = 0
            copy_mask[valid_pos] = 1

            stack_total_observation[window.row_off: row_offset, window.col_off: col_offset] += copy_mask.astype(numpy.uint8)

            # Find all no data in destination STACK image
            stack_raster_where_nodata = numpy.where(
                stack_raster[window.row_off: row_offset, window.col_off: col_offset] == nodata
            )

            # Turns into a 1-dimension
            stack_raster_nodata_pos = numpy.ravel_multi_index(stack_raster_where_nodata,
                                                              stack_raster[window.row_off: row_offset,
                                                              window.col_off: col_offset].shape)

            # Find all valid/cloud in destination STACK image
            raster_where_data = numpy.where(raster != nodata)
            raster_data_pos = numpy.ravel_multi_index(raster_where_data, raster.shape)

            # Match stack nodata values with observation
            # stack_raster_where_nodata && raster_where_data
            intersect_ravel = numpy.intersect1d(stack_raster_nodata_pos, raster_data_pos)

            if len(intersect_ravel):
                where_intersec = numpy.unravel_index(intersect_ravel, raster.shape)
                stack_raster[window.row_off: row_offset, window.col_off: col_offset][where_intersec] = raster[where_intersec]

                provenance_array[window.row_off: row_offset, window.col_off: col_offset][where_intersec] = day_of_year

                if build_clear_observation and is_combined_collection:
                    data_set_block[where_intersec] = datasource_block[where_intersec]

            # Identify what is needed to stack, based in Array 2d bool
            todomask = notdonemask * numpy.invert(bmask)

            # Find all positions where valid data matches.
            clear_not_done_pixels = numpy.where(numpy.logical_and(todomask, numpy.invert(masked.mask)))

            # Override the STACK Raster with valid data.
            stack_raster[window.row_off: row_offset, window.col_off: col_offset][clear_not_done_pixels] = raster[
                clear_not_done_pixels]

            # Mark day of year to the valid pixels
            provenance_array[window.row_off: row_offset, window.col_off: col_offset][
                clear_not_done_pixels] = day_of_year

            if build_clear_observation and is_combined_collection:
                data_set_block[clear_not_done_pixels] = datasource_block[clear_not_done_pixels]

            if apply_valid_range:
                # Apply band limit
                raster_valid_data = raster[raster_where_data]
                saturated_positions_min = numpy.where(raster_valid_data < min_value)
                saturated_positions_max = numpy.where(raster_valid_data > max_value)
                bmask[raster_where_data][saturated_positions_min] = True
                bmask[raster_where_data][saturated_positions_max] = True

            # Update what was done.
            notdonemask = notdonemask * bmask

        if cube_function == 'MED':
            median = numpy.ma.median(stackMA, axis=0).data
            median[notdonemask.astype(numpy.bool_)] = nodata

            median_raster[window.row_off: row_offset, window.col_off: col_offset] = median.astype(profile['dtype'])

        if build_clear_observation:
            count_raster = numpy.ma.count(stackMA, axis=0)

            clear_ob_data_set.dataset.write(count_raster.astype(clear_ob_profile['dtype']), window=window, indexes=1)

            if is_combined_collection:
                datasource.dataset.write(data_set_block, window=window, indexes=1)

    # Close all input dataset
    for order in range(numscenes):
        bandlist[order].close()
        masklist[order].close()

    # Evaluate cloud cover
    efficacy, cloudcover = _qa_statistics(stack_raster, mask=mask_values, compute=False)

    profile.update({
        'tiled': True,
        'interleave': 'pixel',
    })

    # Since count no cloud operator is specific for a band, we must ensure to manipulate data set only
    # for band clear observation to avoid concurrent processes write same data set in disk.
    # TODO: Review how to design it to avoid these IF's statement, since we must stack data set and mask dummy values
    if build_clear_observation:
        clear_ob_data_set.close()
        logging.info('Clear Observation (ClearOb) file generated successfully.')

        total_observation_file = build_cube_path(datacube, period, tile_id, version=version,
                                                 band=TOTAL_OBSERVATION_NAME, composed=True, **kwargs)
        total_observation_profile = profile.copy()
        total_observation_profile.pop('nodata', None)
        total_observation_profile['dtype'] = 'uint8'

        save_as_cog(str(total_observation_file), stack_total_observation, block_size=block_size, **total_observation_profile)
        generate_cogs(str(clear_ob_file_path), str(clear_ob_file_path), block_size=block_size)

        activity['clear_observation_file'] = str(clear_ob_data_set.path)
        activity['total_observation'] = str(total_observation_file)

    if cube_function == 'MED':
        # Close and upload the MEDIAN dataset
        save_as_cog(str(cube_file), median_raster, block_size=block_size, mode='w', **profile)
    else:
        save_as_cog(str(cube_file), stack_raster, block_size=block_size, mode='w', **profile)

        if build_clear_observation:
            provenance_file = build_cube_path(datacube, period, tile_id, version=version,
                                              band=PROVENANCE_NAME, composed=True, **kwargs)
            provenance_profile = profile.copy()
            provenance_profile['nodata'] = PROVENANCE_ATTRIBUTES['nodata']
            provenance_profile['dtype'] = PROVENANCE_ATTRIBUTES['data_type']

            save_as_cog(str(provenance_file), provenance_array, block_size=block_size, **provenance_profile)
            activity['provenance'] = str(provenance_file)

            if is_combined_collection:
                datasource.close()
                generate_cogs(str(dataset_file_path), str(dataset_file_path), block_size=block_size)
                activity['datasource'] = str(dataset_file_path)

    activity['blends'] = {
        cube_function: str(cube_file)
    }

    # Release reference count
    stack_raster = None

    activity['efficacy'] = efficacy
    activity['cloudratio'] = cloudcover

    return activity


def generate_rgb(rgb_file: Path, qlfiles: List[str], input_range, output_range=(0, 255,), **kwargs):
    """Generate a raster file that stack the quick look files into RGB channel."""
    # TODO: Save RGB definition on Database
    with rasterio.open(str(qlfiles[0])) as dataset:
        profile = dataset.profile

    profile['count'] = 3
    profile['dtype'] = 'uint8'
    with rasterio.open(str(rgb_file), 'w', **profile) as dataset:
        for band_index in range(len(qlfiles)):
            with rasterio.open(str(qlfiles[band_index])) as band_dataset:
                windows = band_dataset.block_windows()

                for _, window in windows:
                    data = band_dataset.read(1, window=window)
                    data = linear_raster_scale(data, input_range=input_range, output_range=output_range)

                    dataset.write(data.astype(numpy.uint8), band_index + 1, window=window)

    logging.info(f'Done RGB {str(rgb_file)}')


def concat_path(*entries) -> Path:
    """Concat any path and retrieves a pathlib.Path.

    Note:
        This method resolves the path concatenation when right argument starts with slash /.
        The default python join does not merge any right path when starts with slash.

    Examples:
        >>> print(str(concat_path('/path', '/any/path/')))
        ... '/path/any/path/'
    """
    base = Path('/')

    for entry in entries:
        base /= entry if not str(entry).startswith('/') else str(entry)[1:]

    return base


def is_relative_to(absolute_path: Union[str, Path], *other) -> bool:
    """Return True if the given another path is relative to absolute path.

    Note:
        Adapted from Python 3.9
    """
    try:
        Path(absolute_path).relative_to(*other)
        return True
    except ValueError:
        return False


def _item_prefix(absolute_path: Path, data_dir: str = None) -> Path:
    data_dir = data_dir or Config.DATA_DIR
    if is_relative_to(absolute_path, Config.WORK_DIR):
        relative_prefix = Config.WORK_DIR
    elif is_relative_to(absolute_path, data_dir):
        relative_prefix = data_dir
    else:
        raise ValueError(f'Invalid file prefix for {str(absolute_path)} - ({Config.WORK_DIR}, {data_dir})')

    relative_path = Path(absolute_path).relative_to(relative_prefix)

    return concat_path(Config.ITEM_PREFIX, relative_path)


def publish_datacube(cube: Collection, bands, tile_id, period, scenes, cloudratio, reuse_data_cube=None,
                     srid=SRID_ALBERS_EQUAL_AREA, data_dir=None, **kwargs):
    """Generate quicklook and catalog datacube on database."""
    start_date, end_date = period.split('_')
    data_dir = data_dir or Config.DATA_DIR

    datacube = cube.name
    version = cube.version
    if reuse_data_cube:
        datacube = reuse_data_cube['name']
        version = reuse_data_cube['version']

    format_item_cube = kwargs.get('format_item_cube')
    output = []

    for composite_function in [cube.composite_function.alias]:
        item_id = get_item_id(datacube, version, tile_id, period, fmt=format_item_cube)

        cube_bands = cube.bands

        quick_look_file = build_cube_path(datacube, period, tile_id, version=version, suffix=None, composed=True, **kwargs)

        ql_files = []
        for band in bands:
            ql_files.append(scenes[band][composite_function])

        quick_look_file = generate_quick_look(str(quick_look_file), ql_files, **kwargs)

        if kwargs.get('with_rgb'):
            rgb_file = build_cube_path(datacube, period, tile_id, version=version, band='RGB', composed=True, **kwargs)
            generate_rgb(rgb_file, ql_files, **kwargs)

        map_band_scene = {name: composite_map[composite_function] for name, composite_map in scenes.items()}

        custom_bands = generate_band_indexes(cube, map_band_scene, period, tile_id, reuse_data_cube=reuse_data_cube,
                                             composed=True, **kwargs)
        for name, file in custom_bands.items():
            scenes[name] = {composite_function: str(file)}

        tile = Tile.query().filter(Tile.name == tile_id, Tile.grid_ref_sys_id == cube.grid_ref_sys_id).first()

        files_to_move = []

        with db.session.begin_nested():
            item_data = dict(
                name=item_id,
                collection_id=cube.id,
                tile_id=tile.id,
                start_date=start_date,
                end_date=end_date,
            )

            item, _ = get_or_create_model(Item, defaults=item_data, name=item_id, collection_id=cube.id)
            item.cloud_cover = cloudratio

            item.add_asset('thumbnail', file=str(quick_look_file), role=['thumbnail'],
                           href=str(_item_prefix(Path(quick_look_file), data_dir=data_dir)))

            relative_work_dir_file = Path(quick_look_file).relative_to(Config.WORK_DIR)
            target_publish_dir = Path(data_dir) / relative_work_dir_file.parent
            # Ensure file is writable
            target_publish_dir.mkdir(exist_ok=True, parents=True)

            files_to_move.append((
                str(quick_look_file),  # origin
                str(target_publish_dir / relative_work_dir_file.name)  # target
            ))

            item.start_date = start_date
            item.end_date = end_date

            bbox = to_shape(item.bbox) if item.bbox else None
            footprint = to_shape(item.footprint) if item.footprint else None

            for band in scenes:
                band_model = list(filter(lambda b: b.name == band, cube_bands))

                # Band does not exist on model
                if not band_model:
                    logging.warning('Band {} of {} does not exist on database. Skipping'.format(band, cube.id))
                    continue

                if bbox is None:
                    bbox = raster_extent(str(scenes[band][composite_function]))

                if footprint is None:
                    footprint = raster_convexhull(str(scenes[band][composite_function]))

                item.add_asset(band_model[0].name, file=str(scenes[band][composite_function]), role=['data'],
                               href=str(_item_prefix(scenes[band][composite_function], data_dir=data_dir)),
                               mime_type=COG_MIME_TYPE,
                               is_raster=True)

                destination_file = target_publish_dir / Path(scenes[band][composite_function]).name

                # Move to DATA_DIR
                files_to_move.append((
                    str(scenes[band][composite_function]), # origin
                    str(destination_file) # target
                ))

            item.srid = srid
            if footprint.area > 0.0:
                item.footprint = from_shape(footprint, srid=4326, extended=True)
            item.bbox = from_shape(bbox, srid=4326, extended=True)
            item.save(commit=False)

        db.session.commit()

        for origin_file, destination_file in files_to_move:
            output.append(str(destination_file))
            if str(origin_file) != str(destination_file):
                shutil.move(origin_file, destination_file)

        # Remove the parent ctx directory in WORK_DIR
        cleanup(Path(quick_look_file).parent)

    return output


def publish_merge(bands, datacube, tile_id, date, scenes, reuse_data_cube=None, srid=SRID_ALBERS_EQUAL_AREA,
                  data_dir=None, **kwargs):
    """Generate quicklook and catalog warped datacube on database.

    TODO: Review it with publish_datacube
    """
    data_dir = data_dir or Config.DATA_DIR
    cube_name = datacube.name
    cube_version = datacube.version

    if reuse_data_cube:
        cube_name = reuse_data_cube['name']
        cube_version = reuse_data_cube['version']
        reuse_data_cube['name'] = cube_name

    format_item_cube = kwargs.get('format_item_cube')
    item_id = get_item_id(cube_name, cube_version, tile_id, date, fmt=format_item_cube)

    quick_look_file = build_cube_path(cube_name, date, tile_id, version=cube_version, composed=False, suffix=None, **kwargs)

    cube_bands = datacube.bands
    output = []

    ql_files = []
    for band in bands:
        ql_files.append(scenes['ARDfiles'][band])

    quick_look_file.parent.mkdir(exist_ok=True, parents=True)
    quick_look_file = generate_quick_look(str(quick_look_file), ql_files, **kwargs)

    # Generate VI
    if not kwargs.get('skip_vi_identity'):
        custom_bands = generate_band_indexes(datacube, scenes['ARDfiles'], date, tile_id, reuse_data_cube=reuse_data_cube,
                                             composed=False, **kwargs)
        scenes['ARDfiles'].update(custom_bands)

    tile = Tile.query().filter(Tile.name == tile_id, Tile.grid_ref_sys_id == datacube.grid_ref_sys_id).first()

    files_to_move = []

    with db.session.begin_nested():
        item_data = dict(
            name=item_id,
            # Ensure that data cube id belongs to the original cube, not reused cube.
            collection_id=datacube.id,
            tile_id=tile.id,
            start_date=date,
            end_date=date,
        )

        item, _ = get_or_create_model(Item, defaults=item_data, name=item_id, collection_id=datacube.id)
        item.cloud_cover = scenes.get('cloudratio', 0)

        bbox = to_shape(item.bbox) if item.bbox else None
        footprint = to_shape(item.footprint) if item.footprint else None

        item.add_asset('thumbnail', file=str(quick_look_file), role=['thumbnail'],
                       href=str(_item_prefix(Path(quick_look_file), data_dir=data_dir)))

        relative_work_dir_file = Path(quick_look_file).relative_to(Config.WORK_DIR)
        target_publish_dir = Path(data_dir) / relative_work_dir_file.parent
        # Ensure file is writable
        target_publish_dir.mkdir(exist_ok=True, parents=True)

        files_to_move.append((
            str(quick_look_file),  # origin
            str(target_publish_dir / relative_work_dir_file.name)  # target
        ))

        item.start_date = date
        item.end_date = date

        for band in scenes['ARDfiles']:
            band_model = list(filter(lambda b: b.name == band, cube_bands))

            # Band does not exists on model
            if not band_model:
                logging.warning('Band {} of {} does not exist on database'.format(band, datacube.id))
                continue

            if bbox is None:
                bbox = raster_extent(str(scenes['ARDfiles'][band]))

            if footprint is None:
                footprint = raster_convexhull(str(scenes['ARDfiles'][band]))

            item.add_asset(band_model[0].name, file=str(scenes['ARDfiles'][band]),
                           href=str(_item_prefix(scenes['ARDfiles'][band], data_dir=data_dir)),
                           role=['data'], mime_type=COG_MIME_TYPE, is_raster=True)

            destination_file = target_publish_dir / Path(scenes['ARDfiles'][band]).name

            # Move to DATA_DIR
            files_to_move.append((
                str(scenes['ARDfiles'][band]),  # origin
                str(destination_file)  # target
            ))

        item.srid = srid
        item.bbox = from_shape(bbox, srid=4326, extended=True)
        if footprint.area > 0.0:
            item.footprint = from_shape(footprint, srid=4326, extended=True)
        item.save(commit=False)

    db.session.commit()

    for origin_file, destination_file in files_to_move:
        output.append(str(destination_file))
        if str(origin_file) != str(destination_file):
            shutil.move(origin_file, destination_file)

    cleanup(Path(quick_look_file).parent)

    return output


def cleanup(directory: Union[str, Path]):
    """Cleanup a directory.

    Note:
        Only remove temporary files and rmdir when dir is empty.
    """
    directory = Path(directory)
    for entry in directory.iterdir():
        # Remove any file inside that are temp file
        if entry.is_file() and entry.name.startswith('tmp') and entry.suffix.lower() == '.tif':
            entry.unlink()
    try:
        directory.rmdir()
    except OSError:
        pass


def generate_quick_look(file_path, qlfiles, channel_limits: ChannelLimits = None, **kwargs):
    """Generate quicklook on disk."""
    default_range = 0, 10000
    if channel_limits is None:
        channel_limits = default_range, default_range, default_range

    if len(channel_limits) != 3:
        raise ValueError(f'Invalid value for channels in quicklook. Expects 3 elements (r, g, b), but got {len(channel_limits)}')

    with rasterio.open(qlfiles[0]) as src:
        profile = src.profile

    numlin = 768
    numcol = int(float(profile['width'])/float(profile['height'])*numlin)
    image = numpy.ones((numlin, numcol, len(qlfiles),), dtype=numpy.uint8)
    pngname = '{}.png'.format(file_path)

    nb = 0
    for idx, file in enumerate(qlfiles):
        with rasterio.open(file) as src:
            raster = src.read(1, out_shape=(numlin, numcol))

            # Rescale to 0-255 values
            nodata = raster <= 0
            limit = channel_limits[idx]
            if raster.min() != 0 or raster.max() != 0:
                raster = raster.astype(numpy.float32) / float(limit[1]) * 255.
                raster[raster < 0] = 0
                raster[raster > 255] = 255
            image[:, :, nb] = raster.astype(numpy.uint8) * numpy.invert(nodata)
            nb += 1

    write_png(pngname, image, transparent=(0, 0, 0))
    return pngname


def parse_mask(mask: dict):
    """Parse input mask according to the raster.

    This method expects a dict with contains the following keys:

        clear_data - Array of clear data
        nodata - Cloud mask nodata
        not_clear (Optional) _ List of pixels to be not considered.

    It will read the input array and get all unique values. Make sure to call for cloud file.

    The following section describe an example how to pass the mask values for any cloud processor:

        fmask = dict(
            clear_data=[0, 1],
            not_clear_data=[2, 3, 4],
            nodata=255
        )

        sen2cor = dict(
            clear_data=[4, 5, 6, 7],
            not_clear_data=[2, 3, 8, 9, 10, 11],
            saturated_data=[1],
            nodata=0
        )

    Note:
        It may take too long do parse, according to the raster.
        Not mapped values will be treated as "others" and may not be count in the Clear Observation Band (CLEAROB).

    Args:
        mask (dict): Mapping for cloud masking values.

    Returns:
        dict Representing the mapped values of cloud mask.
    """
    clear_data = numpy.array(mask['clear_data'])
    not_clear_data = numpy.array(mask.get('not_clear_data', []))
    saturated_data = mask.get('saturated_data', [])

    if mask.get('nodata') is None:
        raise RuntimeError('Expected nodata value set to compute data set statistics.')

    nodata = mask['nodata']

    res = dict(
        clear_data=clear_data,
        not_clear_data=not_clear_data,
        saturated_data=saturated_data,
        nodata=nodata,
        bits=mask.get('bits', False)
    )

    if mask.get('saturated_band'):
        res['saturated_band'] = mask['saturated_band']

    return res


def _qa_statistics(raster, mask: dict, compute: bool = False, confidence=None) -> Tuple[float, float]:
    """Retrieve raster statistics efficacy and cloud factor.

    This method uses the evidence of ``mask`` attribute to category the raster
    efficacy and cloud factor using ``clear_data``, ``not_clear_data`` and
    ``saturated_data`` as described in `Temporal Compositing <https://brazil-data-cube.github.io/products/specifications/processing-flow.html#temporal-compositing>`_

    Args:
        raster (numpy.ndarray|numpy.ma.MaskedArray): Image Raster values
        mask (dict): A mask dictionary containing the values described in ``Temporal Compositing``.
            The supported values are:
            - ``clear_data``: Raster values used to be considered as valid data.
            - ``not_clear_data``: Raster values used to be considered as invalid data.
            - ``saturated_data``: Raster values used describe as saturated.
            - ``saturated_band``: Band used to read and match saturated values. Usually referred for
                Landsat. Defaults to ``None``.
            - ``nodata``: Cloud Mask nodata value
            - ``bits``: Flag to deal with Bitwise cloud factor. Defaults to ``False``.

    Note:
        The efficacy is based on `non nodata` pixels.

    Note:
        When the ``bits`` is set on mask parameter, the values referred in ``clear_data`` and ``not_clear_data``
        will be used as Bit factor.

    Returns:
        Tuple[float, float]: Tuple of efficacy and cloud cover, respectively.
    """
    from .image import get_qa_mask

    if compute:
        mask = parse_mask(mask)

    confidence = confidence or mask.get('confidence')

    # Total pixels used to retrieve data efficacy
    total_pixels = raster.size
    if mask['bits']:
        nodata_pixels = raster[raster == mask['nodata']].size
        qa_mask = get_qa_mask(raster,
                              clear_data=mask['clear_data'],
                              not_clear_data=mask['not_clear_data'],
                              nodata=mask['nodata'],
                              confidence=confidence)
        clear_pixels = qa_mask[numpy.invert(qa_mask.mask)].size
        # Since the nodata values is already masked, we should remove the difference
        not_clear_pixels = qa_mask[qa_mask.mask].size - nodata_pixels
    else:
        # Compute how much data is for each class. It will be used as image area
        clear_pixels = raster[numpy.where(numpy.isin(raster, mask['clear_data']))].size
        not_clear_pixels = raster[numpy.where(numpy.isin(raster, mask['not_clear_data']))].size

    # Image area is everything, except nodata.
    image_area = clear_pixels + not_clear_pixels
    not_clear_ratio = 100

    if image_area != 0:
        not_clear_ratio = round(100. * not_clear_pixels / image_area, 2)

    efficacy = round(100. * clear_pixels / total_pixels, 2)

    return efficacy, not_clear_ratio


def build_cube_path(datacube: str, period: str, tile_id: str, version: int, band: str = None,
                    suffix: Union[str, None] = '.tif', prefix=None,
                    format_path_cube: str = None,
                    format_item_cube: str = None,
                    composed: bool = False,
                    **kwargs) -> Path:
    """Retrieve the path to the Data cube file in Brazil Data Cube Cluster.

    The following values are available for ``format_path_cube``:

    - ``datacube``: Data cube name
    - ``prefix``: Prefix for cubes.
    - ``path``: Orbit path from tile id (for native BDC Grids).
    - ``row``: Orbit row from tile id (for native BDC Grids).
    - ``tile_id``: Same value in variable.
    - ``year``: String representation of Start Date Year
    - ``month``: String representation of Start Date Month
    - ``day``: String representation of Start Date Day
    - ``version``: Version string using ``V<Value>``.
    - ``version_legacy``: Legacy string version using the structure ``v{0:03d}`` -> ``v001``.
    - ``period=period``: String period (Start/End) date.
    - ``filename``: Entire Item id using value from ``format_item_cube``.

    Args:
        datacube (str): The data cube base name
        period (str): String representation for Data Period. It may be ``start_date`` for
            Identity Data cubes or ``start_date_end_date`` for temporal composing data cube.
        tile_id (str): The tile identifier as string.
        version (str): String representation for Collection version.
        band (Union[str, None]): Attach a band value into path. Defaults to ``None``.
        suffix (Union[str, None]): Path suffix representing file extension. Defaults to ``.tif``.
        prefix (str): Path prefix for cubes. Defaults to ``Config.WORK_DIR``.
        format_path_cube (Optional[str]): Custom format while building data cube path. Defaults
            to ``{prefix}/{folder}/{datacube:lower}/{version}/{path}/{row}/{year}/{month}/{day}/{filename}``.
        format_item_cube (Optional[str]): Custom format while building data cube item name. Defaults
            to ``{datacube:upper}_V{version}_{tile_id}_{start_date}``.
        composed (bool): Flag to identify cube context (identity or composed). Defaults to ``False``.
    """
    # Default prefix path is WORK_DIR
    prefix = prefix or Config.WORK_DIR
    folder = 'identity'
    if composed:
        folder = 'composed'

    version_str = 'v{0:03d}'.format(int(version))
    path, row = tile_id[:3], tile_id[-3:]
    # Manual start date reference
    year = str(period[:4])
    month = '{0:02d}'.format(int(period[5:7]))
    day = '{0:02d}'.format(int(period[8:10]))

    fmt_kwargs = dict(
        datacube=datacube,
        prefix=prefix,
        path=path, row=row,
        tile_id=tile_id,
        year=year,
        month=month,
        day=day,
        version=f'v{version}',  # New version format
        version_legacy=version_str,
        period=period,
    )

    if format_path_cube is None:
        format_path_cube = '{prefix}/{folder}/{datacube:lower}/{version}/{path}/{row}/{year}/{month}/{day}/{filename}'

    file_name = get_item_id(datacube, version, tile_id, period, fmt=format_item_cube)

    if band is not None:
        file_name = f'{file_name}_{band}'

    if suffix is None:
        suffix = ''

    file_name = f'{file_name}{suffix}'

    fmt_kwargs['filename'] = file_name
    fmt_kwargs['folder'] = folder

    return Path(FORMATTER.format(format_path_cube, **fmt_kwargs))


@contextmanager
def rasterio_access_token(access_token=None):
    """Retrieve a context manager that wraps a temporary file containing the access token to be passed to STAC."""
    with TemporaryDirectory() as tmp:
        options = dict()

        if access_token:
            tmp_file = Path(tmp) / f'{access_token}.txt'
            with open(str(tmp_file), 'w') as f:
                f.write(f'X-Api-Key: {access_token}')
            options.update(GDAL_HTTP_HEADER_FILE=str(tmp_file))

        yield options
