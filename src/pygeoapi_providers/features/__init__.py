import json
from copy import deepcopy
import logging
import xml.etree.ElementTree as ET
from typing import Dict, List, Tuple, Any
from urllib.parse import urljoin, urlsplit, urlunsplit
from osgeo import ogr, osr
from sqlalchemy import Engine, text, select
from sqlalchemy.orm import Session, load_only
from geoalchemy2 import WKBElement
from geoalchemy2.functions import ST_Intersects, ST_MakeEnvelope, ST_Transform
from cachetools import cached, TTLCache, keys
import requests
from pygeoapi.provider.base import ProviderItemNotFoundError
from pygeoapi.provider.sql import PostgreSQLProvider as PostgreSQLProviderBase
from pygeoapi.crs import CrsTransformSpec, get_crs, transform_bbox, DEFAULT_STORAGE_CRS
from .schema import json_schema_to_fields, json_schema_to_collection_schema

ogr.UseExceptions()
osr.UseExceptions()

_sessions_cache = TTLCache(maxsize=640 * 1024, ttl=86400)

_logger = logging.getLogger(__name__)
DEFAULT_CRS = "http://www.opengis.net/def/crs/OGC/1.3/CRS84"


class PostgreSQLProvider(PostgreSQLProviderBase):
    """
    A provider for querying a PostgreSQL database.
      * Supports nonlinear geometry types
      * Supports field mappings for richer JSON schema
      * Caches table IDs for faster creation of fields for previous and next items
      * Improved performance when querying large tables
      * Converts dot-concatenated fields to objects (or flattens with underscores)
      * Fixes a bug related to BBOX filtering
    """

    def __init__(self, provider_def: dict):
        self.storage_crs_uri: str = provider_def.get(
            "storage_crs", DEFAULT_STORAGE_CRS)

        self.schema: str | None = provider_def.get('schema')

        self.field_mappings: Dict[str, Any] = provider_def.get(
            "field_mappings", {})
        
        self.has_curve_geoms: bool = provider_def.get("curve_geoms", False)

        self.excluded_properties: List[str] = provider_def.get(
            "exclude_properties", [])
        
        self.flatten_properties: bool = provider_def.get(
            "flatten_properties", False)

        super().__init__(provider_def)

        self.link_templates = _normalize_link_config(provider_def.get("links"))

        self.links_base_url = (
            provider_def.get("links_base")
            or provider_def.get("links_base_url")
            or provider_def.get("base_url")
        )

    @property
    def fields(self) -> Dict:
        fields = self.get_fields()

        if self.flatten_properties:
            return {key.split(".")[-1]: value for key, value in fields.items()}

        return fields

    def get_fields(self) -> Dict:        
        if self.schema:
            fields = json_schema_to_fields(self.schema)

            if fields:
                return fields

        fields = super().get_fields()
        
        if not self.field_mappings:
            return fields

        for key, value in self.field_mappings.items():
            if key in fields:
                props: Dict = fields[key]
                props.update(value)
        
        return fields

    def get_collection_schema(self) -> Dict | None:
        if self.schema:
            return json_schema_to_collection_schema(self.schema, self.id_field, self.time_field)

        return None

    def query(
        self,
        offset=0,
        limit=10,
        resulttype="results",
        bbox=[],
        datetime_=None,
        properties: List[Tuple[str, str]] = [],
        sortby: List[Dict[str, Any]] = [],
        select_properties: List[str] = [],
        skip_geometry=False,
        q=None,
        filterq=None,
        crs_transform_spec: CrsTransformSpec | None = None,
        **kwargs,
    ):
        """
        Query sql database for all the content.
        e,g: http://localhost:5000/collections/hotosm_bdi_waterways/items?
        limit=1&resulttype=results

        :param offset: starting record to return (default 0)
        :param limit: number of records to return (default 10)
        :param resulttype: return results or hit limit (default results)
        :param bbox: bounding box [minx,miny,maxx,maxy]
        :param datetime_: temporal (datestamp or extent)
        :param properties: list of tuples (name, value)
        :param sortby: list of dicts (property, order)
        :param select_properties: list of property names
        :param skip_geometry: bool of whether to skip geometry (default False)
        :param q: full-text search term(s)
        :param filterq: CQL query as text string
        :param crs_transform_spec: `CrsTransformSpec` instance, optional

        :returns: GeoJSON FeatureCollection
        """
        if self.flatten_properties and properties:
            properties = [
                (self._unflatten_property_name(name), value)
                for name, value in properties
            ]

        property_filters: Any = self._get_property_filters(properties)
        cql_filters: Any = self._get_cql_filters(filterq)
        bbox_filter: Any = self._get_bbox_filter(bbox)
        time_filter: Any = self._get_datetime_filter(datetime_)
        order_by_clauses = self._get_order_by_clauses(sortby, self.table_model)
        selected_properties = self._select_properties_clause(
            select_properties, skip_geometry
        )

        links_base = _determine_links_base_url(kwargs, self.links_base_url)

        with Session(self._engine) as session:
            if resulttype != "hits":
                id_column = getattr(self.table_model, self.id_field)

                ids_cte = (
                    select(id_column.label("id"))
                    .filter(property_filters)
                    .filter(cql_filters)
                    .filter(bbox_filter)
                    .filter(time_filter)
                    .order_by(id_column)
                    .offset(offset)
                    .limit(limit)
                    .cte("ids")
                )                

                results = (
                    session.query(self.table_model)
                    .join(ids_cte, id_column == ids_cte.c.id)
                    .options(selected_properties)
                )
            else:
                results = (
                    session.query(self.table_model)
                    .filter(property_filters)
                    .filter(cql_filters)
                    .filter(bbox_filter)
                    .filter(time_filter)
                    .options(selected_properties)
                )

            response: Dict[str, Any] = {"type": "FeatureCollection"}

            response["features"] = []
            response["numberMatched"] = results.count()
            response["numberReturned"] = 0

            if resulttype == "hits" or not results:
                return response

            target_crs = _get_target_crs(
                crs_transform_spec, self.storage_crs_uri)

            coord_trans = _get_coordinate_transformation(crs_transform_spec)

            crs_uri = (
                crs_transform_spec.target_crs_uri
                if crs_transform_spec
                else self.storage_crs_uri
            )

            _add_geojson_crs(response, crs_uri)

            items = results.order_by(*order_by_clauses)

            for item in items:
                response["numberReturned"] += 1
                response["features"].append(
                    self._create_feature(
                        item, target_crs, coord_trans, select_properties, links_base
                    )
                )

        return response

    def get(
        self,
        identifier,
        crs_transform_spec: CrsTransformSpec | None = None,
        **kwargs,
    ):
        """
        Query the provider for a specific
        feature id e.g: /collections/hotosm_bdi_waterways/items/13990765

        :param identifier: feature id
        :param crs_transform_spec: `CrsTransformSpec` instance, optional

        :returns: GeoJSON FeatureCollection
        """

        with Session(self._engine) as session:
            item = session.get(self.table_model, identifier)

            if item is None:
                msg = f"No such item: {self.id_field}={identifier}."
                raise ProviderItemNotFoundError(msg)

            links_base = _determine_links_base_url(kwargs, self.links_base_url)

            target_crs = _get_target_crs(
                crs_transform_spec, self.storage_crs_uri)

            coord_trans = _get_coordinate_transformation(crs_transform_spec)

            feature = self._create_feature(
                item, target_crs, coord_trans, [], links_base
            )

            crs_uri = (
                crs_transform_spec.target_crs_uri
                if crs_transform_spec
                else self.storage_crs_uri
            )
            _add_geojson_crs(feature, crs_uri)

            if self.properties:
                props: Dict = feature["properties"]
                dropping_keys = deepcopy(props).keys()

                for item in dropping_keys:
                    if item not in self.properties:
                        props.pop(item)

            self._set_prev_and_next(identifier, feature, session)

        return feature

    def _create_feature(
        self,
        item: Any,
        target_crs: str,
        coord_trans: osr.CoordinateTransformation | None,
        select_properties: List[str],
        links_base: str | None = None,
    ) -> Dict[str, Any]:
        feature: Dict[str, Any] = {"type": "Feature"}

        item_dict: Dict[str, Any] = item.__dict__
        item_dict.pop("_sa_instance_state")

        if item_dict.get(self.geom):
            ewkb_elem: WKBElement = item_dict.pop(self.geom)
            geom = self._get_geometry(ewkb_elem)

            if coord_trans:
                geom.Transform(coord_trans)

            if target_crs == "EPSG:4326":
                geom.SwapXY()

            json_str = geom.ExportToJson()

            feature["geometry"] = json.loads(json_str)
        else:
            feature["geometry"] = None

        feature_id = item_dict.pop(self.id_field)

        feature["id"] = feature_id
        properties = {}

        # self._add_mapped_values(item_dict)
        keys = self._get_properties(select_properties)

        for key in keys:
            if key in item_dict:
                properties[key] = item_dict[key]        

        if self.flatten_properties:
            feature["properties"] = self._flatten_properties(properties)
        else:
            feature["properties"] = self._objectify_properties(properties)

        self._add_provider_links(feature, feature_id, links_base)

        return feature

    def _get_bbox_filter(self, bbox: List[float]):
        if not bbox:
            return True

        bbox_crs84 = transform_bbox(bbox, self.storage_crs_uri, DEFAULT_CRS)
        storage_srid = self.storage_crs.to_epsg()
        envelope = ST_Transform(ST_MakeEnvelope(
            *bbox_crs84, 4326), storage_srid)

        geom_column = getattr(self.table_model, self.geom)
        bbox_filter = ST_Intersects(envelope, geom_column)

        return bbox_filter

    def _get_geometry(self, ewkb_elem: WKBElement) -> ogr.Geometry:
        wkb_elem = ewkb_elem.as_wkb()
        geom: ogr.Geometry = ogr.CreateGeometryFromWkb(wkb_elem.data)

        return geom if not self.has_curve_geoms else geom.GetLinearGeometry()

    def _get_properties(self, select_properties: List[str]) -> List[str]:
        keys = self._expand_property_prefixes(
            select_properties) or self.get_fields().keys()
        filtered = [key for key in keys if key not in self.excluded_properties]

        return filtered

    def _expand_property_prefixes(self, names: List[str]) -> List[str]:
        """Expand parent prefixes to their dot-notated child columns.

        E.g. ["arealplanId"] -> ["arealplanId.kommunenummer", "arealplanId.planidentifikasjon", ...]
        """
        if not names:
            return names

        expanded: List[str] = []

        for name in names:
            if name in self.get_fields():
                expanded.append(name)
                continue

            children = [
                key for key in self.get_fields() if key.startswith(f"{name}.")]

            if children:
                expanded.extend(children)
            else:
                expanded.append(name)

        return expanded

    def _select_properties_clause(self, select_properties, skip_geometry=False):
        column_names = list(select_properties or self.get_fields().keys())

        if self.properties:
            column_names = self.properties

        column_names = self._expand_property_prefixes(column_names)

        if not skip_geometry:
            column_names = list(column_names)
            column_names.append(self.geom)

        selected_columns = []

        for name in column_names:
            try:
                selected_columns.append(getattr(self.table_model, name))
            except AttributeError:
                pass

        if not selected_columns:
            return load_only(getattr(self.table_model, self.id_field))

        return load_only(*selected_columns)

    def _unflatten_property_name(self, name: str) -> str:
        """Map a flattened property name back to its dot-notated column name."""
        if name in self.get_fields():
            return name

        for key in self.get_fields():
            if key.split(".")[-1] == name:
                return key

        return name

    def _flatten_properties(self, properties: Dict[str, Any]) -> Dict[str, Any]:
        return {key.split(".")[-1]: value for key, value in properties.items()}

    def _objectify_properties(self, properties: Dict[str, Any]) -> Dict[str, Any]:
        result = {}

        for key, value in properties.items():
            parts = key.split(".")
            current = result

            for part in parts[:-1]:
                if part not in current or not isinstance(current[part], Dict):
                    current[part] = {}

                current = current[part]

            current[parts[-1]] = value

        return result

    def _set_prev_and_next(self, identifier, feature: Dict, session: Session) -> None:
        identifier_str = str(identifier)
        ids = _get_table_ids(self.table_model, self.id_field, session)

        index = _find_identifier_index(ids, identifier_str)

        if index is None:
            cache = getattr(_get_table_ids, "cache", None)
            cache_key = keys.hashkey(self.table_model)

            if cache is not None:
                cache.pop(cache_key, None)

            ids = _get_table_ids(self.table_model, self.id_field, session)
            index = _find_identifier_index(ids, identifier_str)

        if index is None:
            _logger.warning(
                'ID "%s" not found in cached list for %s; skipping prev/next generation.',
                identifier,
                getattr(self.table_model, "__tablename__", self.table_model),
            )
            return

        if len(ids) == 1:
            prev = ids[0]
        elif index == 0:
            prev = ids[-1]
        else:
            prev = ids[index - 1]

        if len(ids) == 1:
            next = ids[0]
        elif index + 1 == len(ids):
            next = ids[0]
        else:
            next = ids[index + 1]

        feature["prev"] = prev
        feature["next"] = next

    # def _add_mapped_values(self, item_dict: Dict) -> None:
    #     if not self.field_mapping_data:
    #         return

    #     for key, data in self.field_mapping_data.items():
    #         if not key in item_dict:
    #             continue

    #         value = item_dict[key]
    #         mapped_value = next(
    #             (tup for tup in data if tup[0] == str(value)), None)
    #         item_dict[key] = mapped_value[1] if mapped_value else value

    def _add_provider_links(
        self, feature: Dict[str, Any], feature_id: Any, links_base: str | None
    ) -> None:
        if not getattr(self, "link_templates", None):
            return

        format_context: Dict[str, Any] = {"id": feature_id}

        properties = feature.get("properties", {})

        if isinstance(properties, dict):
            format_context.update(properties)

        link_candidates: List[Dict[str, Any]] = []

        for template in getattr(self, "link_templates", []):
            rendered = _render_link_template(template, format_context)

            if rendered:
                link_candidates.append(rendered)

        if link_candidates:
            _merge_links(feature, link_candidates, links_base)

    def _get_collection_namespace(self) -> str:
        return f"{self.db_name}.{self.db_search_path[0]}.{self.table}"


def _get_coordinate_transformation(
    crs_transform_spec: CrsTransformSpec | None,
) -> osr.CoordinateTransformation | None:
    if not crs_transform_spec:
        return None

    source: osr.SpatialReference = osr.SpatialReference()
    source.ImportFromWkt(crs_transform_spec.source_crs_wkt)

    target: osr.SpatialReference = osr.SpatialReference()
    target.ImportFromWkt(crs_transform_spec.target_crs_wkt)

    return osr.CoordinateTransformation(source, target)


def _get_target_crs(
    crs_transform_spec: CrsTransformSpec | None, storage_crs: str
) -> str:
    return str(
        get_crs(
            crs_transform_spec.target_crs_uri if crs_transform_spec else storage_crs
        )
    )


def _add_geojson_crs(geojson: Dict[str, Any], crs_uri: str) -> None:
    crs = get_crs(crs_uri)

    if crs.to_string() == "OGC:CRS84":
        return

    geojson["crs"] = {
        "type": "name",
        "properties": {"name": f"urn:ogc:def:crs:EPSG::{crs.to_epsg() or 4326}"},
    }


@cached(
    cache=_sessions_cache,
    key=lambda table_model, id_field, session: keys.hashkey(table_model),
)
def _get_table_ids(table_model, id_field, session: Session) -> List[Any]:
    id_column = getattr(table_model, id_field)
    result = session.query(id_column).order_by(id_column.asc())
    ids = [str(r[0]) for r in result]

    return ids


def _find_identifier_index(ids: List[Any], identifier: str) -> int | None:
    try:
        return ids.index(identifier)
    except ValueError:
        return None


def _determine_links_base_url(
    kwargs: Dict[str, Any], provider_base: str | None
) -> str | None:
    if not isinstance(kwargs, dict):
        kwargs = {}

    candidates: List[str] = []

    request = kwargs.get("request")

    if request is not None:
        for attr in ("url_root", "host_url", "base_url", "url"):
            value = getattr(request, attr, None)

            if callable(value):
                try:
                    value = value()
                except Exception:
                    value = None

            if value:
                candidates.append(str(value))

    for key in ("request_url_root", "request_url", "url_root", "base_url", "url"):
        value = kwargs.get(key)

        if value:
            candidates.append(str(value))

    headers = kwargs.get("headers") or kwargs.get("request_headers")

    if isinstance(headers, dict):
        proto = headers.get(
            "X-Forwarded-Proto") or headers.get("Forwarded-Proto")
        host = headers.get("X-Forwarded-Host") or headers.get("Host")

        if proto and host:
            candidates.append(f"{proto}://{host}/")

        forwarded = headers.get("Forwarded")

        if isinstance(forwarded, str):
            first_entry = forwarded.split(",", 1)[0]
            parts: Dict[str, str] = {}

            for element in first_entry.split(";"):
                if "=" not in element:
                    continue

                key, value = element.split("=", 1)
                parts[key.strip().lower()] = value.strip()

            proto = parts.get("proto")
            host = parts.get("host")

            if proto and host:
                candidates.append(f"{proto}://{host}/")

    if provider_base:
        candidates.append(str(provider_base))

    for candidate in candidates:
        base = _normalize_base_href(candidate)

        if base:
            return base

    return None


def _merge_links(
    feature: Dict[str, Any], candidates: List[Dict[str, Any]], base_href: str | None
) -> None:
    links = feature.setdefault("links", [])

    if not isinstance(links, list):
        return

    existing_links = {
        (link.get("rel"), link.get("href")) for link in links if isinstance(link, dict)
    }

    normalized_base = _normalize_base_href(base_href)
    existing_base = _get_link_base_href(links)
    fallback_base = existing_base or normalized_base

    for candidate in candidates:
        prepared = _prepare_link(candidate, normalized_base, fallback_base)

        if not prepared:
            continue

        key = (prepared.get("rel"), prepared.get("href"))

        if key in existing_links:
            continue

        links.append(prepared)
        existing_links.add(key)


def _get_link_base_href(links: List[Dict[str, Any]]) -> str | None:
    for rel_name in ("self", "collection"):
        for link in links:
            if not isinstance(link, dict):
                continue

            if link.get("rel") != rel_name:
                continue

            href = link.get("href")

            base = _derive_base_href(href)

            if base:
                return base

    return None


def _prepare_link(
    candidate: Dict[str, Any], primary_base: str | None, fallback_base: str | None
) -> Dict[str, Any] | None:
    if not isinstance(candidate, dict):
        return None

    href_value = candidate.get("href")

    if not href_value:
        return None

    prepared = deepcopy(candidate)
    resolved_href = href_value if _is_absolute_href(href_value) else None

    base_candidates: List[str] = []

    for base in (primary_base, fallback_base):
        normalized = _normalize_base_href(base)

        if normalized:
            base_candidates.append(normalized)

    if not resolved_href:
        for base in base_candidates:
            resolved_href = _resolve_link_href(href_value, base)

            if _is_absolute_href(resolved_href):
                break

    if not resolved_href or not _is_absolute_href(resolved_href):
        _logger.warning(
            'Link href "%s" could not be resolved to an absolute URL.', href_value
        )
        return None

    prepared["href"] = resolved_href
    prepared["rel"] = prepared.get("rel") or "related"
    prepared.setdefault("type", "application/json")

    return prepared


def _resolve_link_href(target: str, base_href: str | None) -> str:
    if not target:
        return ""

    target_parts = urlsplit(target)

    if target_parts.scheme:
        return target

    if base_href:
        base_parts = urlsplit(base_href)

        if target_parts.path.startswith("/"):
            combined_path = (base_parts.path.rstrip(
                "/") + target_parts.path) or "/"

            return urlunsplit(
                (
                    base_parts.scheme,
                    base_parts.netloc,
                    combined_path,
                    target_parts.query,
                    target_parts.fragment,
                )
            )

        joined = urljoin(base_href, target)

        if joined:
            return joined

    return target


def _normalize_base_href(base_href: str | None) -> str | None:
    if not base_href:
        return None

    try:
        base = _derive_base_href(str(base_href))
    except Exception:
        return None

    return base


def _derive_base_href(url: str | None) -> str | None:
    if not url:
        return None

    parts = urlsplit(url)

    if not parts.scheme or not parts.netloc:
        return None

    marker = "/collections/"
    path = parts.path or "/"

    if marker in path:
        path = path[: path.index(marker)]

    if not path:
        path = "/"

    path = path.rstrip("/")

    if not path:
        path = "/"

    if not path.endswith("/"):
        path = f"{path}/"

    return urlunsplit((parts.scheme, parts.netloc, path, "", ""))


def _is_absolute_href(href: str | None) -> bool:
    if not href:
        return False

    parts = urlsplit(str(href))

    return bool(parts.scheme and parts.netloc)


def _render_link_template(
    template: Dict[str, Any], context: Dict[str, Any]
) -> Dict[str, Any] | None:
    if not isinstance(template, dict):
        return None

    rendered: Dict[str, Any] = {}

    for key, value in template.items():
        try:
            rendered[key] = _format_template_value(value, context)
        except KeyError as err:
            missing = err.args[0]
            _logger.warning(
                'Link template field "%s" is missing property "%s".', key, missing
            )
            return None
        except Exception as err:
            _logger.warning(
                'Link template field "%s" could not be resolved: %s', key, err
            )
            return None

    if "href" not in rendered:
        return None

    rendered.setdefault("rel", "related")
    rendered.setdefault("type", "application/json")

    return rendered


def _format_template_value(value: Any, context: Dict[str, Any]) -> Any:
    if isinstance(value, str):
        return value.format_map(context)

    if isinstance(value, dict):
        return {
            key: _format_template_value(sub_value, context)
            for key, sub_value in value.items()
        }

    if isinstance(value, list):
        formatted_list: List[Any] = []

        for item in value:
            formatted_list.append(_format_template_value(item, context))

        return formatted_list

    return value


def _normalize_link_config(link_definition: Any) -> List[Dict[str, Any]]:
    templates: List[Dict[str, Any]] = []

    if not link_definition:
        return templates

    if isinstance(link_definition, dict):
        templates.append(deepcopy(link_definition))
        return templates

    if isinstance(link_definition, list):
        for item in link_definition:
            if isinstance(item, dict):
                templates.append(deepcopy(item))

    return templates


@cached(
    cache=_sessions_cache,
    key=lambda field_mappings, namespace, engine, db_search_path: keys.hashkey(
        namespace
    ),
)
def _get_field_mapping_data(
    field_mappings: Dict[str, Dict[str, str]],
    namespace: str,
    engine: Engine,
    db_search_path: str,
) -> Dict[str, List[Tuple]]:
    mapping_data: Dict[str, List[Tuple]] = {}

    if not field_mappings:
        return mapping_data

    codelist_mappings = [
        item for item in field_mappings.items() if "codelist" in item[1]
    ]

    if codelist_mappings:
        codelist_mapping_data = _create_field_mapping_data_from_codelists(
            codelist_mappings
        )
        mapping_data.update(codelist_mapping_data)

    table_mappings = [item for item in field_mappings.items()
                      if "table" in item[1]]

    if table_mappings:
        table_mapping_data = _create_field_mapping_data_from_tables(
            engine, db_search_path, table_mappings
        )
        mapping_data.update(table_mapping_data)

    return mapping_data


def _create_field_mapping_data_from_tables(
    engine: Engine, db_search_path: str, table_mappings: List[Tuple[str, Dict]]
) -> Dict[str, List[Tuple]]:
    mapping_data: Dict[str, List[Tuple]] = {}

    with engine.connect() as connection:
        for key, value in table_mappings:
            try:
                sql = f"SELECT {value.get('id_field')}, {value.get('value_field')} FROM {db_search_path}.{value.get('table')}"
                result = connection.execute(text(sql))
                rows = result.fetchall()
                values = [tuple(row) for row in rows]
                mapping_data[key] = values
            except Exception as err:
                _logger.warning(
                    f"Could not create mapping data from table {value.get('table')}: {err}"
                )

    return mapping_data


def _create_field_mapping_data_from_codelists(
    codelist_mappings: List[Tuple[str, Dict[str, str]]],
) -> Dict[str, List[Tuple]]:
    mapping_data: Dict[str, List[Tuple]] = {}

    for key, value in codelist_mappings:
        url = value.get("codelist")

        if not url:
            continue

        try:
            mapping_data[key] = _get_codelist(url)
        except Exception as err:
            _logger.warning(
                f"Could not create mapping data from codelist {url}: {err}")

    return mapping_data


def _get_codelist(url: str) -> List[Tuple[str, str]]:
    response = requests.get(url)
    response.raise_for_status()

    root = ET.fromstring(response.text)
    ns = {"gml": "http://www.opengis.net/gml/3.2"}
    codelist: List[Tuple[str, str]] = []

    for definition in root.findall("gml:dictionaryEntry/gml:Definition", ns):
        id = definition.findtext("gml:identifier", namespaces=ns)
        name = definition.findtext("gml:name", namespaces=ns)

        if not id or not name:
            continue

        codelist.append((id.strip(), name.strip()))

    codelist.sort(key=lambda entry: entry[0])

    return codelist
