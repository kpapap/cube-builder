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

"""Define Cube Builder Task Activity to track celery execution."""

from datetime import datetime
from typing import Union

from bdc_catalog.models.base_sql import BaseModel, db
# 3rdparty
from sqlalchemy import ARRAY, JSON, Column, Date, Index, Integer, String, Text, text
from sqlalchemy.engine import ResultProxy

from ..config import Config


class Activity(BaseModel):
    """Define a SQLAlchemy model to track celery execution."""

    __tablename__ = 'activities'

    id = Column(Integer, primary_key=True)
    collection_id = Column(String(64), nullable=False)
    warped_collection_id = Column(String(64), nullable=False)
    activity_type = Column('activity_type', String(64), nullable=False)
    period = Column(String(64), nullable=False)
    date = Column(Date, nullable=False)
    tile_id = Column(String, nullable=False)
    status = Column(String(64), nullable=False)
    args = Column('args', JSON)
    tags = Column('tags', ARRAY(String))
    scene_type = Column('scene_type', String)
    band = Column('band', String(64), nullable=False)
    traceback = Column(Text(), nullable=True)

    __table_args__ = (
        Index(None, tile_id),
        Index(None, date),
        Index(None, band),
        Index(None, status),
        Index(None, collection_id),
        Index(None, warped_collection_id),
        {"schema": Config.ACTIVITIES_SCHEMA}
    )

    @classmethod
    def list_merge_files(cls, collection: str, tile: str,
                         start_date: Union[str, datetime],
                         end_date: Union[str, datetime],
                         identity: bool = False) -> ResultProxy:
        """List all merge files used in data cube generation."""
        field = 'collection_id'
        if identity:
            field = 'warped_collection_id'
        sql = """
        SELECT id, tile_id, band, date::VARCHAR as date, collection_id, args->>'file' AS file, args->'dataset'::VARCHAR AS data_set, (elem->>'link')::VARCHAR as link, status, traceback::TEXT
          FROM cube_builder.activities
         CROSS JOIN json_array_elements(args->'assets') elem
         WHERE {} = '{}'
           AND tile_id = '{}'
           AND date BETWEEN '{}'::DATE AND '{}'::DATE
         ORDER BY id
        """.format(
            field,
            collection,
            tile,
            start_date,
            end_date
        )

        res = db.session.execute(text(sql))

        return res.fetchall()
