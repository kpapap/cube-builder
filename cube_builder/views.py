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
"""Define Brazil Data Cube Cube Builder routes."""

from bdc_auth_client.decorators import oauth2
# 3rdparty
from flask import Blueprint, jsonify, request

# Cube Builder
from .celery.utils import list_queues
from .config import Config
from .controller import CubeController
from .forms import (CubeDetailForm, CubeItemsForm, CubeStatusForm, DataCubeForm, DataCubeMetadataForm,
                    DataCubeProcessForm, GridForm, ListCubeForm, PeriodForm)
from .version import __version__

bp = Blueprint('cubes', import_name=__name__)


@bp.route('/', methods=['GET'])
def status():
    """Define a simple route to retrieve Cube-Builder API status."""
    return dict(
        message='Running',
        description='Cube Builder',
        version=__version__
    ), 200


@bp.route('/cube-status', methods=('GET', ))
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["read"], throw_exception=Config.BDC_AUTH_REQUIRED)
def cube_status(**kwargs):
    """Retrieve the cube processing state, which refers to total items and total to be done."""
    form = CubeStatusForm()

    args = request.args.to_dict()

    errors = form.validate(args)

    if errors:
        return errors, 400

    return jsonify(CubeController.get_cube_status(**args))


@bp.route('/cubes', defaults=dict(cube_id=None), methods=['GET'])
@bp.route('/cubes/<cube_id>', methods=['GET'])
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["read"], throw_exception=Config.BDC_AUTH_REQUIRED)
def list_cubes(cube_id, **kwargs):
    """List all data cubes available."""
    if cube_id is not None:
        message, status_code = CubeController.get_cube(cube_id)

    else:
        form = ListCubeForm()
        args = request.args.to_dict()
        errors = form.validate(args)
        if errors:
            return errors, 400
        data = form.load(args)

        message, status_code = CubeController.list_cubes(**data)

    return jsonify(message), status_code


@bp.route('/cubes', methods=['POST'])
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["write"], throw_exception=Config.BDC_AUTH_REQUIRED)
def create_cube(**kwargs):
    """Define POST handler for datacube creation.

    Expects a JSON that matches with ``DataCubeForm``.
    """
    form = DataCubeForm()

    args = request.get_json()

    errors = form.validate(args)

    if errors:
        return errors, 400

    data = form.load(args)

    cubes, status = CubeController.create(data)

    return jsonify(cubes), status


@bp.route('/cubes/<int:cube_id>', methods=['PUT'])
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["write"], throw_exception=Config.BDC_AUTH_REQUIRED)
def update_cube_matadata(cube_id, **kwargs):
    """Define PUT handler for datacube Update.

    Expects a JSON that matches with ``DataCubeMetadataForm``.
    """
    form = DataCubeMetadataForm()

    args = request.get_json()

    errors = form.validate(args)

    if errors:
        return errors, 400

    data = form.load(args)

    message, status = CubeController.update(cube_id, data)

    return jsonify(message), status


@bp.route('/cubes/<cube_id>/tiles', methods=['GET'])
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["read"], throw_exception=Config.BDC_AUTH_REQUIRED)
def list_tiles(cube_id, **kwargs):
    """List all data cube tiles already done."""
    message, status_code = CubeController.list_tiles_cube(cube_id, only_ids=True)

    return jsonify(message), status_code


@bp.route('/cubes/<cube_id>/parameters', methods=['PUT'])
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["write"], throw_exception=Config.BDC_AUTH_REQUIRED)
def update_cube_parameters(cube_id, **kwargs):
    """Update the data cube parameters execution."""
    parameters = request.get_json()

    message = CubeController.configure_parameters(int(cube_id), **parameters)

    return jsonify(message)


@bp.route('/cubes/<cube_id>/complete', methods=['POST'])
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["write"], throw_exception=Config.BDC_AUTH_REQUIRED)
def complete_cube_timeline(cube_id, **kwargs):
    """Complete the data cube missing time steps."""
    result = CubeController.complete_cube_timeline(cube_id)
    return jsonify(result), 200


@bp.route('/cubes/<int:cube_id>/tiles/geom', methods=['GET'])
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["read"], throw_exception=Config.BDC_AUTH_REQUIRED)
def list_tiles_as_features(cube_id, **kwargs):
    """List all tiles as GeoJSON feature."""
    message, status_code = CubeController.list_tiles_cube(int(cube_id))

    return jsonify(message), status_code


@bp.route('/cubes/<int:cube_id>/items', methods=['GET'])
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["read"], throw_exception=Config.BDC_AUTH_REQUIRED)
def list_cube_items(cube_id, **kwargs):
    """List all data cube items."""
    form = CubeItemsForm()

    args = request.args.to_dict()

    errors = form.validate(args)

    if errors:
        return errors, 400

    message, status_code = CubeController.list_cube_items(cube_id, **args)

    return jsonify(message), status_code


@bp.route('/cubes/<int:cube_id>/meta', methods=['GET'])
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["read"], throw_exception=Config.BDC_AUTH_REQUIRED)
def get_cube_meta(cube_id, **kwargs):
    """Retrieve the meta information of a data cube such STAC provider used, collection, etc."""
    message, status_code = CubeController.cube_meta(cube_id)

    return jsonify(message), status_code


@bp.route('/start', methods=['POST'])
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["write"], throw_exception=Config.BDC_AUTH_REQUIRED)
def start_cube(**kwargs):
    """Define POST handler for datacube execution.

    Expects a JSON that matches with ``DataCubeProcessForm``.
    """
    args = request.get_json()

    form = DataCubeProcessForm()

    errors = form.validate(args)

    if errors:
        return errors, 400

    data = form.load(args)
    # For Local Data Sources, there is no reference for collections.
    if data.get('local'):
        data['collections'] = None

    proc = CubeController.trigger_datacube(**data)

    return proc


@bp.route('/list-merges', methods=['GET'])
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["read"], throw_exception=Config.BDC_AUTH_REQUIRED)
def list_merges(**kwargs):
    """Define POST handler for datacube execution.

    Expects a JSON that matches with ``DataCubeProcessForm``.
    """
    args = request.args.to_dict()

    form = CubeDetailForm()
    errors = form.validate(args)
    if errors:
        return errors, 400

    if args['cube_id'].isnumeric():
        args['cube_id'] = int(args['cube_id'])

    res = CubeController.check_for_invalid_merges(**args)

    return res


@bp.route('/grids', defaults=dict(grs_id=None), methods=['GET'])
@bp.route('/grids/<grs_id>', methods=['GET'])
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["read"], throw_exception=Config.BDC_AUTH_REQUIRED)
def list_grs_schemas(grs_id, **kwargs):
    """List all data cube Grids."""
    if grs_id is not None:
        args = request.args.to_dict()
        bbox = args.get('bbox')
        if bbox:
            bbox = [float(elm) for elm in bbox.split(',')]
        tiles = args.get('tiles')
        tiles = tiles.split(',') if tiles else None
        result, status_code = CubeController.get_grs_schema(grs_id, bbox=bbox, tiles=tiles)
    else:
        result, status_code = CubeController.list_grs_schemas()

    return jsonify(result), status_code


@bp.route('/create-grids', methods=['POST'])
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["write"], throw_exception=Config.BDC_AUTH_REQUIRED)
def create_grs(**kwargs):
    """Create the grid reference system using HTTP Post method."""
    form = GridForm()

    args = request.get_json()

    errors = form.validate(args)

    if errors:
        return errors, 400

    cubes, status = CubeController.create_grs_schema(**args)

    return cubes, status


@bp.route('/list-periods', methods=['POST'])
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["read"], throw_exception=Config.BDC_AUTH_REQUIRED)
def list_periods(**kwargs):
    """List data cube periods.

    The user must provide the following query-string parameters:
    - schema: Temporal Schema
    - step: Temporal Step
    - start_date: Start offset
    - last_date: End date offset
    """
    parser = PeriodForm()

    args = request.get_json()

    errors = parser.validate(args)

    if errors:
        return errors, 400

    return CubeController.generate_periods(**args)


@bp.route('/composite-functions', methods=['GET'])
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["read"], throw_exception=Config.BDC_AUTH_REQUIRED)
def list_composite_functions(**kwargs):
    """List all data cube supported composite functions."""
    message, status_code = CubeController.list_composite_functions()

    return jsonify(message), status_code


@bp.route('/tasks', methods=['GET'])
@oauth2(required=Config.BDC_AUTH_REQUIRED, roles=["read"], throw_exception=Config.BDC_AUTH_REQUIRED)
def list_tasks(**kwargs):
    """List all pending and running tasks on celery."""
    queues = list_queues()
    return queues
