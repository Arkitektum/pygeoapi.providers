from os import getenv
import logging
from pathlib import Path
from pygeoapi.util import url_join
from typing import Any, Dict, List, Callable
from cachetools import cached, TTLCache, keys
from geoalchemy2.functions import (ST_Transform, ST_AsMVTGeom, ST_AsMVT,
                                   ST_CurveToLine, ST_Extent, ST_XMax, ST_YMax, ST_XMin, ST_YMin, ST_SetSRID)
from sqlalchemy import (Engine, Label, String, Integer,
                        Float, Boolean, Numeric, Date, DateTime)
from sqlalchemy.dialects.postgresql import UUID, JSON, JSONB
from sqlalchemy.sql import select
from sqlalchemy.orm import Session
from pyproj import CRS
from pygeoapi.crs import get_crs
from pygeoapi.provider.mvt_postgresql import MVTPostgreSQLProvider as MVTPostgreSQLProviderBase

_MEM_CACHE_DAYS = getenv('MVT_CACHE_DAYS')

_logger = logging.getLogger(__name__)

_mem_cache_days = int(_MEM_CACHE_DAYS) if _MEM_CACHE_DAYS else 1
_mvt_cache = TTLCache(maxsize=640 * 1024, ttl=_mem_cache_days * 86400)
_bounds_cache = TTLCache(maxsize=1, ttl=_mem_cache_days * 86400)


class MVTPostgreSQLProvider(MVTPostgreSQLProviderBase):
    def __init__(self, provider_def: Dict):
        MVTPostgreSQLProviderBase.__init__(self, provider_def)

        self._layer: str = provider_def.get("layer", self.table)

    def get_layer(self) -> str:
        return self._layer

    def get_tiles(
        self,
        layer=None,
        tileset=None,
        z=None,
        y=None,
        x=None,
        format_=None
    ) -> bytes:
        z, y, x = map(int, [str(z), str(y), str(x)])

        [tileset_schema] = [
            schema
            for schema in self.get_tiling_schemes()
            if tileset == schema.tileMatrixSet
        ]

        if not self.is_in_limits(tileset_schema, z, x, y):
            return bytes()

        cache_key = str(
            Path(self._get_cache_key()).joinpath(
                f"{tileset}/{z}/{y}/{x}.pbf")
        )

        result = _get_tiles(
            str(layer),
            tileset_schema.tileMatrixSet,
            z,
            y,
            x,
            self.storage_crs,
            tileset_schema.crs,
            self._engine,
            self.table_model,
            self.geom,
            self.fields,
            self.get_envelope,
            cache_key
        )

        return result

    def get_vendor_metadata(
        self,
        dataset,
        server_url,
        layer,
        tileset,
        title,
        description,
        keywords,
        **kwargs
    ) -> Dict[str, Any]:
        service_url = url_join(
            server_url,
            f"collections/{dataset}/tiles/{tileset}",
            "{tileMatrix}/{tileRow}/{tileCol}?f=mvt"
        )

        tilejson = {
            "tilejson": "3.0.0",
            "name": title or dataset,
            "description": description,
            "version": "1.0.0",
            "scheme": "tms",
            "tiles": [service_url],
            "minzoom": self.options["zoom"]["min"],
            "maxzoom": self.options["zoom"]["max"],
        }

        bounds = _get_bounds(self._engine, self.table_model,
                             self.geom, self._get_cache_key())

        if bounds:
            tilejson["bounds"] = bounds

            tilejson["center"] = [
                (bounds[0] + bounds[2]) / 2,
                (bounds[1] + bounds[3]) / 2,
                self.options["zoom"]["min"]
            ]

        tilejson["vector_layers"] = [
            {
                "id": layer,
                "description": description,
                "fields": {
                    name: self._map_field_type(column.type)
                    for name, column in self.get_fields().items()
                }
            }
        ]

        return tilejson

    def _map_field_type(self, field_type) -> str:
        if isinstance(field_type, (String)):
            return "String"
        elif isinstance(field_type, (Integer, Float, Numeric)):
            return "Number"
        elif isinstance(field_type, Boolean):
            return "Boolean"
        elif isinstance(field_type, (Date, DateTime)):
            return "String"
        elif isinstance(field_type, (UUID, JSON, JSONB)):
            return "String"

        return "String"

    def _get_cache_key(self) -> str:
        return f"{self.db_name}/{self.db_search_path[0]}/{self.table}" # type: ignore


@cached(
    cache=_mvt_cache,
    key=lambda layer,
    tileset,
    z,
    y,
    x,
    storage_crs,
    tileset_schema_crs,
    engine,
    table_model,
    geom,
    fields,
    get_envelope_func,
    cache_key: keys.hashkey(cache_key)
)
def _get_tiles(
    layer: str,
    tileset: str,
    z: int,
    y: int,
    x: int,
    storage_crs: CRS,
    tileset_schema_crs: str,
    engine: Engine,
    table_model: Any,
    geom: Any,
    fields: Dict,
    get_envelope_func: Callable[[int, int, int, str], Label],
    cache_key: str
) -> bytes:
    storage_srid = get_crs(storage_crs).to_string()
    out_srid = get_crs(tileset_schema_crs).to_string()
    envelope = get_envelope_func(z, y, x, tileset)

    geom_column = getattr(table_model, geom)

    geom_filter = geom_column.intersects(
        ST_Transform(envelope, storage_srid)  # type: ignore
    )

    mvtgeom = ST_AsMVTGeom(
        ST_Transform(ST_CurveToLine(geom_column), out_srid),
        ST_Transform(envelope, out_srid),
    ).label("mvtgeom")

    mvtrow = select(mvtgeom, *fields.values()
                    ).filter(geom_filter).cte("mvtrow")

    mvtquery = select(ST_AsMVT(mvtrow.table_valued(), layer))

    with Session(engine) as session:
        memview: Any = session.execute(mvtquery).scalar()
        result = bytes(memview) or None

    return result or bytes()


@cached(
    cache=_bounds_cache,
    key=lambda engine,
    table_model,
    geom,
    cache_key: keys.hashkey(cache_key)
)
def _get_bounds(
    engine: Engine,
    table_model: Any,
    geom: Any,
    cache_key: str
) -> List[float] | None:
    geom_column = getattr(table_model, geom)
    extent = ST_SetSRID(ST_Extent(ST_Transform(geom_column, 4326)), 4326)

    stmt = select(
        ST_XMin(extent),
        ST_YMin(extent),
        ST_XMax(extent),
        ST_YMax(extent)
    )

    with Session(engine) as session:
        row = session.execute(stmt).first()

    if not row or row[0] is None:
        return None

    return [row[0], row[1], row[2], row[3]]
