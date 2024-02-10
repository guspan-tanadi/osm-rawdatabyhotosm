#!/usr/bin/python3

# Copyright (c) 2023 Humanitarian OpenStreetMap Team
#
# This file is part of osm_rawdata.
#
#     This is free software: you can redistribute it and/or modify
#     it under the terms of the GNU General Public License as published by
#     the Free Software Foundation, either version 3 of the License, or
#     (at your option) any later version.
#
#     Underpass is distributed in the hope that it will be useful,
#     but WITHOUT ANY WARRANTY; without even the implied warranty of
#     MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#     GNU General Public License for more details.
#
#     You should have received a copy of the GNU General Public License
#     along with osm_rawdata.  If not, see <https:#www.gnu.org/licenses/>.
#
"""Tests for data extract generation."""

import logging
import os
import time

import geojson
import requests

import osm_rawdata as rw
from osm_rawdata.postgres import PostgresClient

log = logging.getLogger(__name__)

rootdir = rw.__path__[0]
if os.path.basename(rootdir) == "osm_rawdata":
    rootdir = "./tests/"


def test_data_extract():
    """Test data extract works with zipped geojson default."""
    pg = PostgresClient("underpass", f"{rootdir}/buildings_extract.yaml")
    aoi_file = open(f"{rootdir}/AOI_small.geojson", "r")
    boundary = geojson.load(aoi_file)
    data_extract = pg.execQuery(boundary)
    assert len(data_extract.get("features")) == 22


def test_fgb_data_extract():
    """Test bind_zip=False flatgeobuf for direct data streaming."""
    # Sleep 3 seconds to reduce API load
    time.sleep(3)

    pg = PostgresClient("underpass", f"{rootdir}/buildings_extract.yaml")
    aoi_file = open(f"{rootdir}/AOI_small.geojson", "r")
    boundary = geojson.load(aoi_file)
    extract_url = pg.execQuery(
        boundary,
        extra_params={
            "fileName": "osm-rawdata-test-extract",
            "outputType": "fgb",
            "bind_zip": False,
        },
        # param options: https://hotosm.github.io/raw-data-api/api/endpoints/#rawdatacurrentparams
    )
    assert extract_url.startswith("http")

    with requests.head(extract_url) as response:
        assert response.status_code == 200
        assert response.headers["Content-Type"] == "binary/octet-stream"
        assert response.headers["Content-Length"] == "10640"
