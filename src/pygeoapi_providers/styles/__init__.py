from urllib.parse import urljoin, urlparse
from urllib.request import url2pathname
from pathlib import Path
import logging
from typing import Any, Dict, List
import requests
from .uri_type import UriType

_logger = logging.getLogger(__name__)

_stylesheet_types = {
    'mapbox': {
        'title': 'Mapbox Style',
        'mime_type': 'application/vnd.mapbox.style+json',
        'version': '8',
        'spec': 'https://docs.mapbox.com/style-spec/guides',
        'format': 'mapbox'
    },
    'se11': {
        'title': 'OGC SE',
        'mime_type': 'application/vnd.ogc.se+xml;version=1.1',
        'version': '1.1',
        'spec': 'https://www.ogc.org/standards/se',
        'format': 'se11'
    },
    'sld10': {
        'title': 'OGC SLD',
        'mime_type': 'application/vnd.ogc.sld+xml;version=1.0',
        'version': '1.0',
        'spec': 'https://www.ogc.org/standards/sld',
        'format': 'sld10'
    }
}


class StyleProvider:
    def __init__(self, provider_def: Dict):
        self.styles: List[Dict[str, Any]] = provider_def['styles']
        self.server_url: str = provider_def['server_url']
        self.base_uri: str | None = provider_def.get('base_uri')

    def get_styles(self) -> Dict[str, Any]:
        styles: List[Dict[str, Any]] = []

        for style in self.styles:
            id: str = style['id']

            links = [
                {
                    'rel': 'describedby',
                    'title': 'Style metadata',
                    'href': f'{self.server_url}/styles/{id}/metadata'
                }
            ]

            stylesheets: List[Dict] = style['stylesheets']

            for styleheet in stylesheets:
                type = styleheet['type']

                links.append({
                    'rel': 'stylesheet',
                    'type': _stylesheet_types[type]['mime_type'],
                    'title': f'Style in {_stylesheet_types[type]["title"]} format',
                    'href': f'{self.server_url}/styles/{id}?f={_stylesheet_types[type]["format"]}'
                })

            styles.append({
                'id': id,
                'title': style.get('title'),
                'links': links
            })

        return {
            'styles': styles
        }

    def get_style(self, style_id: str) -> Dict[str, Any] | None:
        styles: List[Dict] = self.get_styles().get('styles', [])
        style = next(
            (style for style in styles if style['id'] == style_id), None)

        return style

    def get_style_metadata(self, style_id: str) -> Dict[str, Any] | None:
        style = next(
            (style for style in self.styles if style['id'] == style_id), None)

        if not style:
            return None

        metadata = {
            'id': style['id'],
            'title': style['title']
        }

        description = style.get('description')

        if description:
            metadata['description'] = description

        keywords = style.get('keywords')

        if keywords:
            metadata['keywords'] = keywords

        metadata['scope'] = 'style'

        version = style.get('version')

        if version:
            metadata['version'] = version

        stylesheets: List[Dict[str, Any]] = style['stylesheets']
        metadata['stylesheets'] = []

        for styleheet in stylesheets:
            type = _stylesheet_types[styleheet['type']]

            metadata['stylesheets'].append({
                'title': type['title'],
                'version': type['version'],
                'specification': type['spec'],
                'native': styleheet['native'],
                'link': {
                    'href': f'{self.server_url}/styles/{style_id}?f={type["format"]}',
                    'rel': 'stylesheet',
                    'type': type['mime_type']
                }
            })

        layers: List[Dict[str, Any]] = style['layers']
        metadata['layers'] = []

        for layer in layers:
            metadata['layers'].append({
                'id': layer['id'],
                'type': layer['type']
            })

        return metadata

    def get_style_definition(self, style_id: str, format_: str) -> str | None:
        style = next(
            (style for style in self.styles if style['id'] == style_id), None)

        if not style:
            return None

        stylesheets: List[Dict[str, Any]] = style.get('stylesheets', [])
        stylesheet = next(
            (stylesheet for stylesheet in stylesheets if stylesheet['type'] == format_), None)

        if not stylesheet:
            return None

        uri: str = stylesheet['uri']

        uri_type = self._get_uri_type(
            self.base_uri) if self.base_uri else self._get_uri_type(uri)

        if uri_type == UriType.HTTP_URL:
            return self._get_style_from_http(self.base_uri, uri)

        base_uri = self.base_uri

        if uri_type == UriType.FILE_URL:
            if self.base_uri:
                parsed_url = urlparse(self.base_uri)
                base_uri = url2pathname(parsed_url.path)
            else:
                parsed_url = urlparse(uri)
                uri = url2pathname(parsed_url.path)

        return self._get_style_from_path(base_uri, uri)

    def get_style_preview(self, style_id: str):
        raise NotImplementedError()

    def _get_style_from_path(self, base_uri: str | None, uri: str) -> str | None:
        path = Path(base_uri).joinpath(uri) if base_uri else Path(uri)

        if not path.exists():
            return None

        with open(path, 'r') as file:
            return file.read()

    def _get_style_from_http(self, base_uri: str | None, uri: str) -> str | None:
        url = urljoin(base_uri.rstrip('/') + '/', uri) if base_uri else uri

        try:
            response = requests.get(url)
            response.raise_for_status()

            return response.text
        except Exception as err:
            _logger.warning(f'Could not fetch style definition: {url}', err)
            return None

    def _get_uri_type(self, uri: str) -> UriType:
        parsed = urlparse(uri)

        if parsed.scheme in ('http', 'https'):
            return UriType.HTTP_URL

        if parsed.scheme == 'file':
            return UriType.FILE_URL

        return UriType.PATH
