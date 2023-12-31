#
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

"""Define Cube Builder forms used to validate both data input and data serialization."""

from bdc_catalog.models import Band, Collection, GridRefSys, db
from marshmallow import Schema, fields, pre_load, validate
from marshmallow.validate import OneOf, Regexp, ValidationError
from marshmallow_sqlalchemy import auto_field
from marshmallow_sqlalchemy.schema import SQLAlchemyAutoSchema
from rasterio.dtypes import dtype_ranges

from cube_builder.constants import IDENTITY


class CollectionForm(SQLAlchemyAutoSchema):
    """Form definition for Model Collection."""

    class Meta:
        """Internal meta information of Form interface."""

        model = Collection
        sqla_session = db.session
        exclude = ('spatial_extent', )


class GridRefSysForm(SQLAlchemyAutoSchema):
    """Form definition for the model GrsSchema."""

    id = fields.String(dump_only=True)

    class Meta:
        """Internal meta information of form interface."""

        model = GridRefSys
        sqla_session = db.session
        exclude = ('table_id', )


class GridForm(Schema):
    """Form model to generate Hierarchical Grid."""

    names = fields.List(fields.String(required=True), required=True)
    description = fields.String()
    projection = fields.String(required=True, load_only=True)
    meridian = fields.Integer(required=True, load_only=True)
    shape = fields.List(fields.Integer, required=True)
    tile_factor = fields.List(fields.List(fields.Integer), required=True)
    bbox = fields.List(fields.Float, required=True, load_only=True)
    srid = fields.Integer(required=True, load_only=True)


class BandForm(SQLAlchemyAutoSchema):
    """Represent the BDC-Catalog Band model."""

    collection_id = auto_field()

    class Meta:
        """Internal meta information of form interface."""

        model = Band
        sqla_session = db.session
        exclude = []


INVALID_CUBE_NAME = 'Invalid data cube name. Expected only letters and numbers.'
SUPPORTED_DATA_TYPES = list(dtype_ranges.keys())


class BandDefinition(Schema):
    """Define a simple marshmallow structure for data cube bands on creation."""

    name = fields.String(required=True, allow_none=False)
    common_name = fields.String(required=True, allow_none=False)
    data_type = fields.String(required=True, allow_none=False, validate=OneOf(SUPPORTED_DATA_TYPES))
    nodata = fields.Float(required=False, allow_none=False)
    metadata = fields.Dict(required=False, allow_none=False)


def validate_mask(value):
    """Validate data cube definition for mask property."""
    if not value.get('clear_data'):
        raise ValidationError('Missing property "clear_data" in the "mask".')


class CustomMaskDefinition(Schema):
    """Define a custom mask."""

    clear_data = fields.List(fields.Integer, required=True, allow_none=False, validate=validate.Length(min=1))
    not_clear_data = fields.List(fields.Integer, required=False, allow_none=False)
    nodata = fields.Integer(required=True, allow_none=False)
    saturated_data = fields.List(fields.Integer, required=False, allow_none=False)
    saturated_band = fields.String(required=False, allow_none=False)
    bits = fields.Boolean(required=False, allow_none=False, dump_default=False)


class CustomScaleSchema(Schema):
    """Represent the Band attribute scale factor and scale mult."""

    add = fields.Float(required=True, allow_none=False, allow_nan=False)
    mult = fields.Float(required=True, allow_none=False, allow_nan=False)


class CubeParametersSchema(Schema):
    """Represent the data cube parameters used to be attached to the cube execution."""

    mask = fields.Nested(CustomMaskDefinition, required=False, allow_none=False, many=False)
    reference_day = fields.Integer(required=False, allow_none=False)
    histogram_matching = fields.Bool(required=False, allow_none=False)
    no_post_process = fields.Bool(required=False, allow_none=False)
    local = fields.String(required=False, allow_none=False)
    recursive = fields.Boolean(required=False, allow_none=False)
    format = fields.String(required=False, allow_none=False)
    pattern = fields.String(required=False, allow_none=False)
    band_map = fields.Dict(required=False, allow_none=False)
    channel_limits = fields.List(
        fields.List(fields.Integer, required=True, validate=validate.Length(min=2, max=2)),
        required=False,
        validate=validate.Length(min=3, max=3)
    )
    sentinel_safe = fields.String(required=False, allow_none=False, dump_default=False)
    """Flag to determine the usage of cube builder and generation data cube from
    `Sentinel-2 Safe Format <https://sentinels.copernicus.eu/web/sentinel/user-guides/sentinel-2-msi/data-formats>`_.

    Use to specify which key described in STAC Item Asset contains the ``zip`` file.
    Defaults to ``None``."""
    combined = fields.Boolean(required=False, allow_none=False)
    resampling = fields.String(required=False, allow_none=False, default="nearest")
    scale = fields.Nested(CustomScaleSchema, required=False, allow_none=False)


class DataCubeForm(Schema):
    """Define parser for datacube creation."""

    datacube = fields.String(required=True, allow_none=False, validate=Regexp('^[a-zA-Z0-9-_]*$', error=INVALID_CUBE_NAME))
    datacube_identity = fields.String(required=False, allow_none=False, validate=Regexp('^[a-zA-Z0-9-_]*$', error=INVALID_CUBE_NAME))
    grs = fields.String(required=True, allow_none=False)
    resolution = fields.Integer(required=True, allow_none=False)
    temporal_composition = fields.Dict(required=True, allow_none=False)
    bands_quicklook = fields.List(fields.String, required=True, allow_none=False)
    composite_function = fields.String(required=True, allow_none=False)
    bands = fields.Nested(BandDefinition, required=True, allow_none=False, many=True)
    quality_band = fields.String(required=False, allow_none=False)
    indexes = fields.Nested(BandDefinition, many=True)
    metadata = fields.Dict(required=True, allow_none=True)
    description = fields.String(required=True, allow_none=False)
    version = fields.Integer(required=True, allow_none=False, dump_default=1)
    title = fields.String(required=True, allow_none=False)
    # Set cubes as public by dump_default.
    public = fields.Boolean(required=False, allow_none=False, dump_default=True)
    # Is Data cube generated from Combined Collections?
    is_combined = fields.Boolean(required=False, allow_none=False, dump_default=False)
    parameters = fields.Nested(CubeParametersSchema, required=True, allow_none=False, many=False)

    @pre_load
    def validate_fields(self, data, **kwargs):
        """Ensure that both indexes and quality band is present in attribute 'bands'.

        Seeks for quality_band in attribute 'bands' and set as `common_name`.

        Raises:
            ValidationError when a band inside indexes or quality_band is duplicated with attribute bands.
        """
        indexes = data['indexes']

        band_names = [b['name'] for b in data['bands']]

        for band_index in indexes:
            if band_index['name'] in band_names:
                raise ValidationError(f'Duplicated band name in indices {band_index["name"]}')

        if data['composite_function'] != IDENTITY and data.get('quality_band') is None:
            raise ValidationError(f'Quality band is required for {data["composite_function"]}.')

        if 'quality_band' in data and data.get('quality_band') is not None:
            if data['quality_band'] not in band_names:
                raise ValidationError(f'Quality band "{data["quality_band"]}" not found in key "bands"')

            band = next(filter(lambda band: band['name'] == data['quality_band'], data['bands']))
            band['common_name'] = 'quality'

        if 'temporal_schema' in data:
            import json
            import pkgutil

            import bdc_catalog
            from jsonschema import draft7_format_checker, validate
            content = pkgutil.get_data(bdc_catalog.__name__, 'jsonschemas/collection-temporal-composition-schema.json')
            schema = json.loads(content)
            try:
                schema['$id'] = schema['$id'].replace('#', '')
                validate(instance=data['temporal_schema'], schema=schema, format_checker=draft7_format_checker)
            except Exception as e:
                raise

        return data


class DataCubeMetadataForm(Schema):
    """Define parser for datacube updation."""

    metadata = fields.Dict(required=False, allow_none=True)
    description = fields.String(required=False, allow_none=False)
    title = fields.String(required=False, allow_none=False)
    public = fields.Boolean(required=False, allow_none=False, dump_default=True)
    bands = fields.Nested(BandForm, required=False, many=True)


class DataCubeProcessForm(Schema):
    """Define parser for datacube generation."""

    datacube = fields.String(required=True, allow_none=False)
    collections = fields.List(fields.String, required=False, allow_none=False)
    tiles = fields.List(fields.String, required=True, allow_none=False)
    start_date = fields.Date()
    end_date = fields.Date()
    bands = fields.List(fields.String, required=False)
    force = fields.Boolean(required=False, dump_default=False)
    with_rgb = fields.Boolean(required=False, dump_default=False)
    token = fields.String(required=False, allow_none=True)
    stac_url = fields.String(required=False, allow_none=True)
    shape = fields.List(fields.Integer(required=False))
    block_size = fields.Integer(required=False, dump_default=512)
    # Reuse data cube from another data cube
    reuse_from = fields.String(required=False, allow_none=True)
    histogram_matching = fields.Boolean(required=False, dump_default=False)
    mask = fields.Dict()
    local = fields.String(required=False, allow_none=False)
    recursive = fields.Boolean(required=False, allow_none=False)
    format = fields.String(required=False, allow_none=False)
    pattern = fields.String(required=False, allow_none=False)


class PeriodForm(Schema):
    """Define parser for Data Cube Periods."""

    schema = fields.String(required=True, allow_none=False)
    step = fields.Integer(required=True)
    unit = fields.String(required=True)
    start_date = fields.String(required=False)
    last_date = fields.String(required=False)
    cycle = fields.Dict(required=False, allow_none=True)
    intervals = fields.List(fields.String, required=False, allow_none=True)


class CubeStatusForm(Schema):
    """Parser for access data cube status resource."""

    cube_name = fields.String(required=True, allow_none=False)


class CubeItemsForm(Schema):
    """Parser for access data cube items resource."""

    tiles = fields.String(required=False)
    bbox = fields.String(required=False)
    start = fields.String(required=False)
    end = fields.String(required=False)
    page = fields.Integer(required=False)
    per_page = fields.Integer(required=False)


class CubeDetailForm(Schema):
    """Parser for List Merges identity."""

    cube_id = fields.String(required=True)
    tile_id = fields.String(required=True)
    start_date = fields.Date(required=True)
    end_date = fields.Date(required=True)


class ListCubeForm(Schema):
    """Form to filter User input to list cubes.

    The values supported are defined as:
    - ``name``: as cube name. The value will be persisted using SQL Like
    - ``collection_type``: collection type identifier. It also supports ``all`` to represent ``cube`` and ``mosaic``.
    - ``public``: data cube is public. Defaults to ``True``.
    """

    name = fields.String(required=False, allow_none=False)
    collection_type = fields.String(required=False, allow_none=False)
    public = fields.Boolean(required=False, dump_default=True)
