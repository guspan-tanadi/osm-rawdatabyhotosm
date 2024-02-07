#!/usr/bin/python3

# Copyright (c) 2022 Humanitarian OpenStreetMap Team
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU Affero General Public License as
# published by the Free Software Foundation, either version 3 of the
# License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU Affero General Public License for more details.

# You should have received a copy of the GNU Affero General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

# Humanitarian OpenStreetmap Team
# 1100 13th Street NW Suite 800 Washington, D.C. 20005
# <info@hotosm.org>

import argparse
import json
import logging
import os
import sys
import time
import zipfile
from io import BytesIO
from pathlib import Path
from sys import argv
from typing import Optional, Union

import geojson
import psycopg2
import requests
from geojson import Feature, FeatureCollection, Polygon
from shapely import wkt
from shapely.geometry import Polygon, shape

# Find the other files for this project
import osm_rawdata as rw
from osm_rawdata.config import QueryConfig

rootdir = rw.__path__[0]

# Instantiate logger
log = logging.getLogger(__name__)


def uriParser(source):
    """Parse a URI into it's components.

    Args:
        source (str): The URI string for the database connection

    Returns:
        dict: The URI split into components

    """
    dbhost = None
    dbname = None
    dbuser = None
    dbpass = None
    dbport = None

    # if dbhost is 'localhost' then this tries to
    # connect to that hostname's tcp/ip port. If dbhost
    # is None, the datbase connection is done locally
    # through the named pipe.
    colon = source.find(":")
    rcolon = source.rfind(":")
    atsign = source.find("@")
    slash = source.find("/")
    # If nothing but a string, then it's a local postgres database
    # that doesn't require a user or password to login.
    if colon < 0 and atsign < 0 and slash < 0:
        dbname = source
    # Get the database name, which is always after the slash
    if slash > 0:
        dbname = source[slash + 1 :]
    # The user field is either between the beginning of the string,
    # and either a colon or atsign as the end.
    if colon > 0:
        dbuser = source[:colon]
    if colon < 0 and atsign > 0:
        dbuser = source[:atsign]
    # The password field is between a colon and the atsign
    if colon > 0 and atsign > 0:
        dbpass = source[colon + 1 : atsign]
    # The hostname for the database is after an atsign, and ends
    # either with the end of the string or a slash.
    if atsign > 0:
        if rcolon > 0 and rcolon > atsign:
            dbhost = source[atsign + 1 : rcolon]
        elif slash > 0:
            dbhost = source[atsign + 1 : slash]
        else:
            dbhost = source[atsign + 1 :]
    # rcolon is only above zero if there is a port number
    if rcolon > 0 and rcolon > atsign:
        if slash > 0:
            dbport = source[rcolon + 1 : slash]
        else:
            dbport = source[rcolon + 1 :]
            # import epdb; epdb.st()
    if colon > 0 and atsign < 0 and slash > 0:
        dbpass = source[colon + 1 : slash]

    if not dbhost:
        dbhost = "localhost"

        # print(f"{source}\n\tcolon={colon} rcolon={rcolon} atsign={atsign} slash={slash}")
    return {"dbname": dbname, "dbhost": dbhost, "dbuser": dbuser, "dbpass": dbpass, "dbport": dbport}


class DatabaseAccess(object):
    def __init__(
        self,
        dburi: str,
    ):
        """This is a class to setup a database connection.

        Args:
            dburi (str): The URI string for the database connection
        """
        self.dbshell = None
        self.dbcursor = None
        self.uri = uriParser(dburi)
        if self.uri["dbname"] == "underpass":
            # Authentication data
            # self.auth = HTTPBasicAuth(self.user, self.passwd)

            # Use a persistant connect, better for multiple requests
            self.session = requests.Session()
            self.url = os.getenv("UNDERPASS_API_URL", "https://api-prod.raw-data.hotosm.org/v1")
            self.headers = {"accept": "application/json", "Content-Type": "application/json"}
        else:
            log.info(f"Opening database connection to: {self.uri['dbname']}")
            connect = "PG: dbname=" + self.uri["dbname"]
            if "dbname" in self.uri and self.uri["dbname"] is not None:
                connect = f"dbname={self.uri['dbname']}"
            elif "dbhost" in self.uri and self.uri["dbhost"] == "localhost" and self.uri["dbhost"] is not None:
                connect = f"host={self.uri['dbhost']} dbname={self.uri['dbname']}"
            if "dbuser" in self.uri and self.uri["dbuser"] is not None:
                connect += f" user={self.uri['dbuser']}"
            if "dbpass" in self.uri and self.uri["dbpass"] is not None:
                connect += f" password={self.uri['dbpass']}"
            # log.debug(f"Connecting with: {connect}")
            try:
                self.dbshell = psycopg2.connect(connect)
                self.dbshell.autocommit = True
                self.dbcursor = self.dbshell.cursor()
                if self.dbcursor.closed != 0:
                    log.error(f"Couldn't open cursor in {self.uri['dbname']}")
            except Exception as e:
                log.error(f"Couldn't connect to database: {e}")

    def __del__(self):
        """Close any open connections to Postgres."""
        if self.dbshell:
            self.dbshell.close()

    def createJson(
        self,
        config: QueryConfig,
        boundary: Polygon,
        allgeom: bool = False,
    ):
        """This class generates a JSON file, which is used for remote access
        to an OSM raw database using the Underpass schema.

        Args:
            config (QueryConfig): The config data from the query config file
            boundary (Polygon): The boundary polygon
            allgeom (bool): Whether to return centroids or all the full geometry

        Returns:
            (FeatureCollection): the json data
        """
        feature = dict()
        feature["geometry"] = boundary

        filters = dict()
        filters["tags"] = dict()
        # filters["tags"]["all_geometry"] = dict()

        # This only effects the output file
        geometrytype = list()
        # for table in config.config['tables']:
        if len(config.config["select"]["nodes"]) > 0 or len(config.config["where"]["nodes"]) > 0:
            geometrytype.append("point")
        if len(config.config["select"]["ways_line"]) > 0 or len(config.config["where"]["ways_line"]) > 0:
            geometrytype.append("line")
        if len(config.config["select"]["ways_poly"]) > 0 or len(config.config["where"]["ways_poly"]) > 0:
            geometrytype.append("polygon")
        feature["geometryType"] = geometrytype

        tables = {"nodes": "point", "ways_poly": "polygon", "ways_line": "line"}
        # The database tables to query
        # if tags exists, then only query those fields
        join_or = {
            "point": [],
            "polygon": [],
            "line": [],
        }
        join_and = {
            "point": [],
            "polygon": [],
            "line": [],
        }
        filters["tags"] = {
            "point": {"join_or": {}, "join_and": {}},
            "polygon": {"join_or": {}, "join_and": {}},
            "line": {"join_or": {}, "join_and": {}},
        }
        for table in config.config["where"].keys():
            for item in config.config["where"][table]:
                key = list(item.keys())[0]
                if item["op"] == "or":
                    join_or[tables[table]].append(key)
                if item["op"] == "and":
                    join_and[tables[table]].append(key)
                if "not null" in item.get(key, []):
                    filters["tags"][tables[table]]["join_or"][key] = []
                    filters["tags"][tables[table]]["join_and"][key] = []
                else:
                    filters["tags"][tables[table]]["join_or"][key] = item[key]
                    filters["tags"][tables[table]]["join_and"][key] = item[key]
        feature.update({"filters": filters})

        attributes = list()
        for table, data in config.config["select"].items():
            for value in data:
                [[k, v]] = value.items()
                if k not in attributes:
                    attributes.append(k)

        # Whether to dump centroids or polygons
        if "centroid" in config.config:
            feature["centroid"] = true
        return json.dumps(feature)

    def createSQL(
        self,
        config: QueryConfig,
        allgeom: bool = True,
    ):
        """This class generates the SQL to query a local postgres database.

        Args:
            config (QueryConfig): The config data from the query config file
            allgeom (bool): Whether to return centroids or all the full geometry

        Returns:
            (FeatureCollection): the json
        """
        sql = list()
        query = ""
        for table in config.config["tables"]:
            select = "SELECT "
            if allgeom:
                select += "ST_AsText(geom)"
            else:
                select += "ST_AsText(ST_Centroid(geom))"
            select += ", osm_id, version, "
            for entry in config.config["select"][table]:
                for k1, v1 in entry.items():
                    select += f"tags->>'{k1}', "
            select = select[:-2]

            join_or = list()
            join_and = list()
            for entry in config.config["where"][table]:
                # print(entry)
                if "op" not in entry:
                    pass
                op = entry["op"]
                for k, v in entry.items():
                    if k == "op":
                        continue
                    if op == "or":
                        # print(f"1: {k}=\'{v}\' OR ")
                        join_or.append(entry)
                    elif op == "and":
                        # print(f"2: {k}=\'{v}\' AND ")
                        join_and.append(entry)
            # jor = '('
            jor = ""
            for entry in join_or:
                for k, v in entry.items():
                    # Check if v is a non-empty list
                    if isinstance(v, list) and v:
                        if isinstance(v[0], list):
                            # It's an array of values
                            value = str(v[0])
                            any = f"ANY(ARRAY{value})"
                            jor += f"tags->>'{k}'={any} OR "
                            continue
                    if k == "op":
                        continue
                    if len(v) == 1:
                        if v[0] == "not null":
                            v1 = "IS NOT NULL"
                        else:
                            v1 = f"='{v[0]}'"
                    elif len(v) > 0:
                        v1 = f" IN {str(tuple(v))}"
                    else:
                        v1 = "IS NOT NULL"
                    jor += f"tags->>'{k}' {v1} OR "
            # print(f"JOR: {jor}")

            jand = ""
            for entry in join_and:
                for k, v in entry.items():
                    if k == "op":
                        continue
                    if len(v) == 1:
                        if v[0] == "not null":
                            v1 = "IS NOT NULL"
                        else:
                            v1 = f"='{v[0]}'"
                    elif len(v) > 0:
                        v1 = f" IN {str(tuple(v))}"
                    else:
                        v1 = "IS NOT NULL AND"
                    jand += f"tags->>'{k}' {v1} AND "
            # print(f"JAND: {jand}")
            query = f"{select} FROM {table} WHERE {jor} {jand}".rstrip()
            # if query[len(query)-5:] == ' OR  ':
            # print(query[:query.rfind(' ')])
            sql.append(query[: query.rfind(" ")])

        return sql

    def createTable(
        self,
        sql: str,
    ):
        """Create a table in the database

        Args:
            sqlfile (str): The SQL

        Returns:
            (bool): The table creation status
        """
        log.info("Creating table schema")
        result = self.dbcursor.execute(sql)

        # path = Path(sqlfile)
        # sql = f"INSERT INTO schemas(schema, version) VALUES('{sqlfile.stem}', 1.0)"
        # result = self.pg.dbcursor.execute(sql)

        return True

    def queryLocal(
        self,
        query: str,
        allgeom: bool = True,
        boundary: Polygon = None,
    ):
        """This query a local postgres database.

        Args:
            query (str): The SQL query to execute
            allgeom (bool): Whether to return centroids or all the full geometry
            boundary (Polygon): The boundary polygon

        Returns:
                query (FeatureCollection): the results of the query
        """
        features = list()
        # if no boundary, it's already been setup
        if boundary:
            sql = f"DROP VIEW IF EXISTS ways_view;CREATE VIEW ways_view AS SELECT * FROM ways_poly WHERE ST_CONTAINS(ST_GeomFromEWKT('SRID=4326;{boundary.wkt}'), geom)"
            self.dbcursor.execute(sql)
            sql = f"DROP VIEW IF EXISTS nodes_view;CREATE VIEW nodes_view AS SELECT * FROM nodes WHERE ST_CONTAINS(ST_GeomFromEWKT('SRID=4326;{boundary.wkt}'), geom)"
            self.dbcursor.execute(sql)
            sql = f"DROP VIEW IF EXISTS lines_view;CREATE VIEW lines_view AS SELECT * FROM ways_line WHERE ST_CONTAINS(ST_GeomFromEWKT('SRID=4326;{boundary.wkt}'), geom)"
            self.dbcursor.execute(sql)
            sql = f"DROP VIEW IF EXISTS relations_view;CREATE TEMP VIEW relations_view AS SELECT * FROM nodes WHERE ST_CONTAINS(ST_GeomFromEWKT('SRID=4326;{boundary.wkt}'), geom)"
            self.dbcursor.execute(sql)

            if query.find(" ways_poly ") > 0:
                query = query.replace("ways_poly", "ways_view")
            elif query.find(" ways_line ") > 0:
                query = query.replace("ways_line", "lines_view")
            elif query.find(" nodes ") > 0:
                query = query.replace("nodes", "nodes_view")
            elif query.find(" relations ") > 0:
                query = query.replace("relations", "relations_view")

        # log.debug(query)
        self.dbcursor.execute(query)
        try:
            result = self.dbcursor.fetchall()
            log.debug("SQL Query returned %d records" % len(result))
        except:
            return FeatureCollection(features)

        # If there is no config file, don't modify the results
        if len(self.qc.config["where"]["ways_poly"]) == 0 and len(self.qc.config["where"]["nodes"]) == 0:
            return result

        for item in result:
            if len(item) <= 1 and len(result) == 1:
                return result
                # break
            geom = wkt.loads(item[0])
            tags = dict()
            tags["id"] = item[1]
            tags["version"] = item[2]
            i = 3
            # If there are no tables, we're using a custom SQL query
            if len(self.qc.config["tables"]) > 0:
                # map the value in the select to the values returns for them.
                for _table, values in self.qc.config["select"].items():
                    for entry in values:
                        if i == len(item):
                            break
                        [[k, v]] = entry.items()
                        if item[i] is not None:
                            tags[k] = item[i]
                        i += 1
            else:
                # Figure out the tags from the custom SELECT
                end = query.find("FROM")
                res = query[:end].split(" ")
                # This should be the geometry
                geom = wkt.loads(item[0])
                # This should be the OSM ID
                tags[res[2][:-1]] = item[1]
                # This should be the version
                tags[res[3][:-1]] = item[2]
            features.append(Feature(geometry=geom, properties=tags))
        return FeatureCollection(features)
        # return features

    def queryRemote(
        self,
        query: str,
    ):
        """This queries a remote postgres database using the FastAPI
        backend to the HOT Export Tool.

        Args:
            query (str): The JSON query to execute

        Returns:
            (FeatureCollection): the results of the query
        """
        # Send the request to raw data api
        result = None

        url = f"{self.url}/snapshot/"
        try:
            result = self.session.post(url, data=query, headers=self.headers)
            result.raise_for_status()
        except requests.exceptions.HTTPError:
            if result is not None:
                error_dict = result.json()
                error_dict["status_code"] = result.status_code
                log.error(f"Failed to get extract from Raw Data API: {error_dict}")
                return error_dict
            else:
                log.error("Failed to make request to raw data API")

        if result is None:
            log.error("Raw Data API did not return a response. Skipping.")
            return None

        if result.status_code != 200:
            error_message = result.json().get("detail")[0].get("msg")
            log.error(f"{error_message}")
            return None

        task_id = result.json().get("task_id")
        task_query_url = f"{self.url}/tasks/status/{task_id}"
        log.debug(f"Raw Data API Query URL: {task_query_url}")

        polling_interval = 2  # Initial polling interval in seconds
        max_polling_duration = 600  # Maximum duration for polling in seconds (10 minutes)
        elapsed_time = 0

        while elapsed_time < max_polling_duration:
            result = self.session.get(task_query_url, headers=self.headers)
            result_json = result.json()

            if result_json.get("status") == "PENDING":
                # Adjust polling frequency after the first minute
                if elapsed_time > 60:
                    polling_interval = 10  # Poll every 10 seconds after the first minute

                # Wait before polling again
                log.debug(f"Waiting {polling_interval} seconds before polling API again...")
                time.sleep(polling_interval)
                elapsed_time += polling_interval

            elif result_json.get("status") == "SUCCESS":
                break

        else:
            # Maximum polling duration reached
            log.error(f"{max_polling_duration} second elapsed. Aborting data extract.")
            return None

        zip_url = result_json["result"]["download_url"]
        result = self.session.get(zip_url, headers=self.headers)
        fp = BytesIO(result.content)
        zfp = zipfile.ZipFile(fp, "r")
        zfp.extract("Export.geojson", "/tmp/")
        # Now take that taskid and hit /tasks/status url with get
        data = zfp.read("Export.geojson")
        os.remove("/tmp/Export.geojson")
        return json.loads(data)


class PostgresClient(DatabaseAccess):
    """Class to handle SQL queries for the categories."""

    def __init__(
        self,
        uri: str,
        config: Optional[Union[str, BytesIO]] = None,
        # output: str = None
    ):
        """This is a client for a postgres database.

        Args:
            uri (str): The URI string for the database connection.
            config (str, BytesIO): The query config file path or BytesIO object.
                Currently only YAML format is accepted if BytesIO is passed.

        Returns:
            (bool): Whether the data base connection was sucessful
        """
        super().__init__(uri)
        self.qc = QueryConfig()

        if config:
            # filespec string passed
            if isinstance(config, str):
                path = Path(config)
                if not path.exists():
                    raise FileNotFoundError(f"Config file does not exist {config}")
                with open(config, "rb") as config_file:
                    config_data = BytesIO(config_file.read())
                if path.suffix == ".json":
                    config_type = "json"
                elif path.suffix == ".yaml":
                    config_type = "yaml"
                else:
                    log.error(f"Unsupported file format: {config}")
                    raise ValueError(f"Invalid config {config}")

            # BytesIO object passed
            elif isinstance(config, BytesIO):
                config_data = config
                config_type = "yaml"

            else:
                log.warning(f"Config input is invalid for PostgresClient: {config}")
                raise ValueError(f"Invalid config {config}")

            # Parse the config
            if config_type == "json":
                self.qc.parseJson(config_data)
            elif config_type == "yaml":
                self.qc.parseYaml(config_data)

    def createDB(self, dburi: uriParser):
        """Setup the postgres database connection.

        Args:
            dburi (str): The URI string for the database connection

        Returns:
            status (bool): Whether the data base connection was sucessful
        """
        sql = f"CREATE DATABASE IF NOT EXISTS {self.dbname}"
        self.dbcursor.execute(sql)
        result = self.dbcursor.fetchall()
        log.info("Query returned %d records" % len(result))
        # result = subprocess.call("createdb", uri.dbname)

        # Add the extensions needed
        sql = "CREATE EXTENSION postgis; CREATE EXTENSION hstore;"
        self.dbcursor.execute(sql)
        result = self.dbcursor.fetchall()
        log.info("Query returned %d records" % len(result))
        return True

    def execQuery(
        self,
        boundary: FeatureCollection,
        customsql: str = None,
        allgeom: bool = True,
    ):
        """This class generates executes the query using a local postgres
        database, or a remote one that uses the Underpass schema.

        Args:
            boundary (FeatureCollection): The boundary polygon
            customsql (str): Don't create the SQL, use the one supplied
            allgeom (bool): Whether to return centroids or all the full geometry

        Returns:
                query (FeatureCollection): the json
        """
        log.info("Extracting features from Postgres...")

        if "features" in boundary:
            # FIXME: ideally this should support multipolygons
            poly = boundary["features"][0]["geometry"]
        else:
            poly = boundary["geometry"]
        wkt = shape(poly)

        if self.dbshell:
            if not customsql:
                sql = self.createSQL(self.qc, allgeom)
            else:
                sql = [customsql]
            alldata = list()
            for query in sql:
                # print(query)
                result = self.queryLocal(query, allgeom, wkt)
                if len(result) > 0:
                    alldata += result["features"]
            collection = FeatureCollection(alldata)
        else:
            json_config = self.createJson(self.qc, poly, allgeom)
            collection = self.queryRemote(json_config)
        return collection


def main():
    """This main function lets this class be run standalone by a bash script."""
    parser = argparse.ArgumentParser(
        prog="postgres",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Make data extract from OSM",
        epilog="""
This program extracts data from a local postgres data, or the remote Underpass
one. A boundary polygon is used to define the are to be covered in the extract.
Optionally a data file can be used.

        """,
    )
    parser.add_argument("-v", "--verbose", nargs="?", const="0", help="verbose output")
    parser.add_argument("-u", "--uri", default="underpass", help="Database URI")
    parser.add_argument("-b", "--boundary", required=True, help="Boundary polygon to limit the data size")
    parser.add_argument("-s", "--sql", help="Custom SQL query to execute against the database")
    parser.add_argument("-a", "--all", help="All the geometry or just centroids")
    parser.add_argument("-c", "--config", help="The config file for the query (json or yaml)")
    parser.add_argument("-o", "--outfile", default="extract.geojson", help="The output file")
    args = parser.parse_args()

    if len(argv) <= 1 or (args.sql is None and args.config is None):
        parser.print_help()
        quit()

    # if verbose, dump to the terminal.
    if args.verbose is not None:
        log.setLevel(logging.DEBUG)
        ch = logging.StreamHandler(sys.stdout)
        ch.setLevel(logging.DEBUG)
        formatter = logging.Formatter("%(threadName)10s - %(name)s - %(levelname)s - %(message)s")
        ch.setFormatter(formatter)
        log.addHandler(ch)

    infile = open(args.boundary, "r")
    poly = geojson.load(infile)
    if args.uri is not None:
        log.info("Using a Postgres database for the data source")
        if args.sql:
            pg = PostgresClient(args.uri)
            sql = open(args.sql, "r")
            result = pg.execQuery(poly, sql.read())
            log.info(f"Custom Query returned {len(result['features'])} records")
        else:
            pg = PostgresClient(args.uri, args.config)
            result = pg.execQuery(poly)
            log.info(f"Canned Query returned {len(result['features'])} records")

        outfile = open(args.outfile, "w")
        geojson.dump(result, outfile)

        log.debug(f"Wrote {args.outfile}")


if __name__ == "__main__":
    """This is just a hook so this file can be run standlone during development."""
    main()
