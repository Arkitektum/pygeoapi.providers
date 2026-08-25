import copy
import json
import logging
from urllib.parse import urlparse
from urllib.request import url2pathname
from pathlib import Path
from typing import Any, Dict
import requests
from .uri_type import UriType

_logger = logging.getLogger(__name__)

_GEOM_FORMATS = {
    'point': 'geometry-point',
    'multipoint': 'geometry-multipoint',
    'linestring': 'geometry-linestring',
    'multilinestring': 'geometry-multilinestring',
    'polygon': 'geometry-polygon',
    'multipolygon': 'geometry-multipolygon',
    'geometrycollection': 'geometry-geometrycollection',
    'geometry': 'geometry-any'
}


def json_schema_to_fields(
    schema_uri: str
) -> Dict[str, Any] | None:
    schema = _load_jsonschema(schema_uri)

    if schema:
        return _create_fields(schema)

    return None


def _create_fields(schema: Dict[str, Any]) -> Dict[str, Any]:
    fields = {}

    def walk(props: Dict[str, Any], prefix: str = ''):
        for name, defn in props.items():
            if not isinstance(defn, dict):
                continue

            key = f'{prefix}.{name}' if prefix else name

            if _is_geometry(defn) or '$ref' in defn:
                continue

            if defn.get('type') == 'object' and 'properties' in defn:
                walk(defn['properties'], key)
                continue

            items = defn.get('items', {})
            if (defn.get('type') == 'array'
                    and isinstance(items, dict)
                    and items.get('type') == 'object'
                    and 'properties' in items):
                walk(items['properties'], key)
                continue

            field = copy.deepcopy(defn)

            if isinstance(field.get('type'), list):
                field['type'] = next(
                    (t for t in field['type'] if t != 'null'), 'string')

            field.setdefault('type', 'string')

            fields[key] = field

    walk(schema.get('properties', schema))
    return fields


def json_schema_to_collection_schema(
    schema_uri: str,
    id_field: str,
    time_field: str | None,
    geometry_field: str | None = 'geometry'
) -> Dict[str, Any] | None:
    schema = _load_jsonschema(schema_uri)

    if schema:
        return _create_collection_schema(schema, id_field, time_field, geometry_field)

    return None


def _create_collection_schema(
    schema: Dict[str, Any],
    id_field: str,
    time_field: str | None,
    geometry_field: str | None
) -> Dict[str, Any]:
    schema = copy.deepcopy(schema)

    def _traverse(node: Dict[str, Any]):
        properties: Dict[str, Dict[str, Any]] = node.get('properties', {})

        for key, prop_schema in properties.items():
            if id_field and key == id_field:
                prop_schema['x-ogc-role'] = 'id'

            elif time_field and key == time_field:
                prop_schema['x-ogc-role'] = 'primary-instant'

            elif geometry_field and key == geometry_field:
                ref: str | None = prop_schema.pop('$ref', None)
                fmt = 'geometry-any'

                if ref:
                    geom_type = ref.rstrip('/').rsplit('/', 1)[-1]
                    geom_type = geom_type.removesuffix('.json').lower()
                    fmt = _GEOM_FORMATS.get(geom_type, 'geometry-any')

                prop_schema['x-ogc-role'] = 'primary-geometry'
                prop_schema['format'] = fmt

            if 'properties' in prop_schema:
                _traverse(prop_schema)

    _traverse(schema)

    return schema


def _is_geometry(defn: Dict[str, Any]) -> bool:
    ref: str = defn.get('$ref', '')

    if ref:
        basename = ref.rstrip('/').rsplit('/', 1)[-1].removesuffix('.json')
        return basename.lower() in {
            'point', 'multipoint', 'linestring', 'multilinestring',
            'polygon', 'multipolygon', 'geometrycollection', 'geometry'}

    return (defn.get('x-ogc-role') == 'primary-geometry'
            or str(defn.get('format', '')).startswith('geometry'))


def _load_jsonschema(schema_uri: str) -> Dict[str, Any] | None:
    uri_type = _get_uri_type(schema_uri)

    if uri_type == UriType.HTTP_URL:
        json_str = _get_schema_from_http(schema_uri)
    else:
        if uri_type == UriType.FILE_URL:
            parsed_url = urlparse(schema_uri)
            schema_uri = url2pathname(parsed_url.path)

        json_str = _get_schema_from_path(schema_uri)

    return json.loads(json_str) if json_str else None


def _get_schema_from_path(schema_uri: str) -> str | None:
    path = Path(schema_uri)

    if not path.exists():
        return None

    with open(path, 'r') as file:
        return file.read()


def _get_schema_from_http(url: str) -> str | None:
    try:
        response = requests.get(url)
        response.raise_for_status()

        return response.text
    except Exception as err:
        _logger.warning(f'Could not JSON schema: {url}', err)
        return None


def _get_uri_type(uri: str) -> UriType:
    parsed = urlparse(uri)

    if parsed.scheme in ('http', 'https'):
        return UriType.HTTP_URL

    if parsed.scheme == 'file':
        return UriType.FILE_URL

    return UriType.PATH


__all__ = ['json_schema_to_collection_schema', 'json_schema_to_fields']
