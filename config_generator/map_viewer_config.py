from collections import OrderedDict
import json
import os

from .permissions_query import PermissionsQuery
from .qgs_reader import QGSReader
from .service_config import ServiceConfig


class MapViewerConfig(ServiceConfig):
    """MapViewerConfig class

    Generate Map Viewer service config and permissions.
    """

    # lookup for edit geometry types:
    #     PostGIS geometry type -> QWC2 edit geometry type
    EDIT_GEOM_TYPES = {
        'POINT': 'Point',
        'MULTIPOINT': 'MultiPoint',
        'LINESTRING': 'LineString',
        'MULTILINESTRING': 'MultiLineString',
        'POLYGON': 'Polygon',
        'MULTIPOLYGON': 'MultiPolygon'
    }

    # lookup for edit field types:
    #     PostgreSQL data_type -> QWC2 edit field type
    EDIT_FIELD_TYPES = {
        'bigint': 'number',
        'boolean': 'boolean',
        'character varying': 'text',
        'date': 'date',
        'double precision': 'text',
        'file': 'file',
        'integer': 'number',
        'numeric': 'number',
        'real': 'text',
        'smallint': 'number',
        'text': 'text',
        'time': 'time',
        'timestamp with time zone': 'date',
        'timestamp without time zone': 'date',
        'uuid': 'text'
    }

    def __init__(self, tenant_path, generator_config, capabilities_reader,
                 config_models, service_config, logger):
        """Constructor

        :param str tenant_path: Path to config files of tenant
        :param obj generator_config: ConfigGenerator config
        :param CapabilitiesReader capabilities_reader: CapabilitiesReader
        :param ConfigModels config_models: Helper for ORM models
        :param obj service_config: Additional service config
        :param Logger logger: Logger
        """
        super().__init__(
            'mapViewer',
            'https://github.com/qwc-services/qwc-map-viewer/raw/master/schemas/qwc-map-viewer.json',
            service_config,
            logger
        )

        self.tenant_path = tenant_path
        self.capabilities_reader = capabilities_reader
        self.config_models = config_models
        self.permissions_query = PermissionsQuery(config_models, logger)
        # helper method alias
        self.permitted_resources = self.permissions_query.permitted_resources

        self.qgis_projects_base_dir = generator_config.get(
            'qgis_projects_base_dir', '/tmp/'
        )

        # keep track of theme IDs for uniqueness
        self.theme_ids = []

        # group counter
        self.groupCounter = 0

        self.default_theme = None

    def config(self):
        """Return service config."""
        # get base config
        config = super().config()

        config['service'] = 'map-viewer'

        resources = OrderedDict()
        config['resources'] = resources

        # collect resources from QWC2 config, capabilities and ConfigDB
        resources['qwc2_config'] = self.qwc2_config()
        resources['qwc2_themes'] = self.qwc2_themes()

        # copy index.html
        self.copy_index_html()

        return config

    def permissions(self, role):
        """Return service permissions for a role.

        :param str role: Role name
        """
        # NOTE: use ordered keys
        permissions = OrderedDict()

        # collect permissions from ConfigDB
        session = self.config_models.session()

        # NOTE: WMS service permissions collected by OGC service config
        permissions['wms_services'] = []
        permissions['background_layers'] = self.permitted_background_layers(
            role
        )
        # NOTE: Data permissions collected by Data service config
        permissions['data_datasets'] = []
        permissions['viewer_tasks'] = self.permitted_viewer_tasks(
            role, session
        )
        permissions['theme_info_links'] = self.permitted_theme_info_links(
            role, session
        )
        permissions['plugin_data'] = self.permitted_plugin_data_resources(
            role, session
        )

        session.close()

        return permissions

    # service config

    def qwc2_config(self):
        """Collect QWC2 application configuration from config.json."""
        # NOTE: use ordered keys
        qwc2_config = OrderedDict()

        # additional service config
        cfg_generator_config = self.service_config.get('generator_config', {})
        cfg_qwc2_config = cfg_generator_config.get('qwc2_config', {})

        # collect restricted menu items from ConfigDB
        qwc2_config['restricted_viewer_tasks'] = self.restricted_viewer_tasks()

        # read QWC2 config.json
        config = OrderedDict()
        try:
            config_file = cfg_qwc2_config.get(
                'qwc2_config_file', 'config.json'
            )
            with open(config_file) as f:
                # parse config JSON with original order of keys
                config = json.load(f, object_pairs_hook=OrderedDict)
        except Exception as e:
            self.logger.critical("Could not load QWC2 config.json:\n%s" % e)
            config['ERROR'] = str(e)

        # remove service URLs
        service_urls = [
            'authServiceUrl',
            'editServiceUrl',
            'elevationServiceUrl',
            'featureReportService',
            'mapInfoService',
            'permalinkServiceUrl',
            'searchDataServiceUrl',
            'searchServiceUrl'
        ]
        for service_url in service_urls:
            config.pop(service_url, None)

        qwc2_config['config'] = config

        return qwc2_config

    def restricted_viewer_tasks(self):
        """Collect restricted viewer tasks from ConfigDB."""
        session = self.config_models.session()
        viewer_tasks = self.permissions_query.non_public_resources(
            'viewer_task', session
        )
        session.close()

        return sorted(list(viewer_tasks))

    def qwc2_themes(self):
        """Collect QWC2 themes configuration from capabilities,
        and edit config from ConfigDB."""
        # NOTE: use ordered keys
        qwc2_themes = OrderedDict()

        # additional service config
        cfg_generator_config = self.service_config.get('generator_config', {})
        cfg_qwc2_themes = cfg_generator_config.get('qwc2_themes', {})

        # QWC2 themes config
        themes_config = self.capabilities_reader.themes_config
        themes_config_themes = themes_config.get('themes', {})

        # reset theme IDs,  default theme and group counter
        self.theme_ids = []
        self.default_theme = None
        self.groupCounter = 0

        # collect resources from capabilities
        themes = OrderedDict()
        themes['title'] = 'root'

        # collect theme items
        items = []
        for item in themes_config_themes.get('items', []):
            theme_item = self.theme_item(item)
            if theme_item is not None:
                items.append(theme_item)
        themes['items'] = items

        # collect theme groups
        groups = []
        for group in themes_config_themes.get('groups', []):
            groups.append(self.theme_group(group))
        themes['subdirs'] = groups

        themes['defaultTheme'] = self.default_theme
        themes['externalLayers'] = themes_config_themes.get(
            'externalLayers', []
        )
        themes['backgroundLayers'] = themes_config_themes.get(
            'backgroundLayers', []
        )
        for backgroundLayer in themes['backgroundLayers']:
            backgroundLayer["attribution"] = {
                "Title": backgroundLayer["attribution"] if "attribution" in backgroundLayer else None,
                "OnlineResource": backgroundLayer["attributionUrl"] if "attributionUrl" in backgroundLayer else None
            }
            backgroundLayer.pop("attributionUrl", None)

        themes['pluginData'] = themes_config_themes.get('pluginData', {})
        themes['themeInfoLinks'] = themes_config_themes.get(
            'themeInfoLinks', []
        )

        themes['defaultWMSVersion'] = themes_config.get(
            'defaultWMSVersion', '1.3.0'
        )
        themes['defaultScales'] = themes_config.get('defaultScales')
        themes['defaultPrintScales'] = themes_config.get('defaultPrintScales')
        themes['defaultPrintResolutions'] = themes_config.get(
            'defaultPrintResolutions'
        )
        themes['defaultPrintGrid'] = themes_config.get('defaultPrintGrid')

        qwc2_themes['themes'] = themes

        return qwc2_themes

    def theme_group(self, cfg_group):
        """Recursively collect theme item group.

        :param obj theme_group: Themes config group
        """
        # NOTE: use ordered keys
        group = OrderedDict()
        self.groupCounter += 1
        group['id'] = "g%d" % self.groupCounter
        group['title'] = cfg_group.get('title')

        # collect sub theme items
        items = []
        for item in cfg_group.get('items', []):
            theme_item = self.theme_item(item)
            if theme_item is not None:
                items.append(theme_item)
        group['items'] = items

        # recursively collect sub theme groups
        subgroups = []
        for subgroup in cfg_group.get('groups', []):
            subgroups.append(self.theme_group(subgroup))
        group['subdirs'] = subgroups

        return group

    def theme_item(self, cfg_item):
        """Collect theme item from capabilities.

        :param obj cfg_item: Themes config item
        """
        # NOTE: use ordered keys
        item = OrderedDict()

        # additional service config
        cfg_config = self.service_config.get('config', {})
        ogc_service_url = cfg_config.get(
            'ogc_service_url', '/ows/'
        ).rstrip('/') + '/'

        # get capabilities
        service_name = self.capabilities_reader.service_name(cfg_item['url'])
        cap = self.capabilities_reader.wms_capabilities.get(service_name)
        if cap is None:
            self.logger.warning(
                "Skipping theme item '%s': Could not get capabilities for %s" %
                (cfg_item.get('title', ""), cfg_item['url'])
            )
            return None

        root_layer = cap.get('root_layer', {})

        name = service_name

        item['id'] = self.unique_theme_id(name)
        item['name'] = name

        if cfg_item.get('default', False) is True:
            # set default theme
            self.default_theme = item['id']

        # title from themes config or capabilities
        title = cfg_item.get('title', cap.get('title'))
        if title is None:
            title = root_layer.get('title', name)
        item['title'] = title

        item['description'] = cfg_item.get('description', '')

        # URL relative to OGC service
        item['wms_name'] = name
        item['url'] = "%s%s" % (ogc_service_url, name)

        attribution = OrderedDict()
        attribution['Title'] = cfg_item.get('attribution')
        attribution['OnlineResource'] = cfg_item.get('attributionUrl')
        item['attribution'] = attribution

        item['abstract'] = cap.get('abstract', '')
        item['keywords'] = cap.get('keywords', '')

        item['mapCrs'] = cfg_item.get('mapCrs', 'EPSG:3857')
        self.set_optional_config(cfg_item, 'additionalMouseCrs', item)

        bbox = OrderedDict()
        bbox['crs'] = 'EPSG:4326'
        bbox['bounds'] = root_layer.get('bbox')
        item['bbox'] = bbox

        if 'extent' in cfg_item:
            initial_bbox = OrderedDict()
            initial_bbox['crs'] = cfg_item.get('mapCrs', 'EPSG:4326')
            initial_bbox['bounds'] = cfg_item.get('extent')
            item['initialBbox'] = initial_bbox
        else:
            item['initialBbox'] = item['bbox']

        # get search layers from searchProviders
        search_providers = cfg_item.get('searchProviders', [])
        search_layers = {}
        for search_provider in search_providers:
            if (
                'provider' in search_provider
                and search_provider.get('provider') == 'solr'
            ):
                search_layers = search_provider.get('layers', {})
                break

        # collect layers
        layers = []
        for layer in root_layer.get('layers', []):
            layers.append(self.collect_layers(layer, search_layers))
        item['sublayers'] = layers
        item['expanded'] = True
        item['drawingOrder'] = cap.get('drawing_order', [])

        self.set_optional_config(cfg_item, 'externalLayers', item)
        self.set_optional_config(cfg_item, 'backgroundLayers', item)

        print_templates = cap.get('print_templates', [])
        if print_templates:
            if 'printLabelBlacklist' in cfg_item:
                # NOTE: copy print templates to not overwrite original config
                print_templates = [
                    template.copy() for template in print_templates
                ]
                for print_template in print_templates:
                    # filter print labels
                    labels = [
                        label for label in print_template['labels']
                        if label not in cfg_item['printLabelBlacklist']
                    ]
                    print_template['labels'] = labels
            item['print'] = print_templates

        self.set_optional_config(cfg_item, 'printLabelConfig', item)
        self.set_optional_config(cfg_item, 'printLabelForSearchResult', item)

        self.set_optional_config(cfg_item, 'extraLegendParameters', item)

        self.set_optional_config(cfg_item, 'skipEmptyFeatureAttributes', item)

        if "minSearchScaleDenom" in cfg_item.keys():
            item["minSearchScaleDenom"] = cfg_item.get("minSearchScaleDenom")
        elif "minSearchScale" in cfg_item.keys():  # Legacy name
            item["minSearchScaleDenom"] = cfg_item.get("minSearchScale")

        self.set_optional_config(cfg_item, "visibility", item)

        item['searchProviders'] = cfg_item.get('searchProviders', [])

        # edit config
        item['editConfig'] = self.edit_config(name, cfg_item)

        self.set_optional_config(cfg_item, 'watermark', item)
        self.set_optional_config(cfg_item, 'config', item)
        self.set_optional_config(cfg_item, 'mapTips', item)
        self.set_optional_config(cfg_item, 'userMap', item)
        self.set_optional_config(cfg_item, 'pluginData', item)
        self.set_optional_config(cfg_item, 'themeInfoLinks', item)

        # TODO: generate thumbnail
        item['thumbnail'] = "img/mapthumbs/%s" % cfg_item.get(
            'thumbnail', 'default.jpg'
        )

        self.set_optional_config(cfg_item, 'version', item)
        self.set_optional_config(cfg_item, 'format', item)
        self.set_optional_config(cfg_item, 'tiled', item)

        # TODO: availableFormats
        item['availableFormats'] = [
            'image/jpeg',
            'image/png',
            'image/png; mode=16bit',
            'image/png; mode=8bit',
            'image/png; mode=1bit'
        ]
        # TODO: infoFormats
        item['infoFormats'] = [
            'text/plain',
            'text/html',
            'text/xml',
            'application/vnd.ogc.gml',
            'application/vnd.ogc.gml/3.1.1'
        ]

        self.set_optional_config(cfg_item, 'scales', item)
        self.set_optional_config(cfg_item, 'printScales', item)
        self.set_optional_config(cfg_item, 'printResolutions', item)
        self.set_optional_config(cfg_item, 'printGrid', item)

        return item

    def unique_theme_id(self, name):
        """Return unique theme id for item name.

        :param str name: Theme item name
        """
        theme_id = name

        # make sure id is unique
        suffix = 1
        while theme_id in self.theme_ids:
            # add suffix to name
            theme_id = "%s_%s" % (name, suffix)
            suffix += 1

        # add to used IDs
        self.theme_ids.append(theme_id)

        return theme_id

    def set_optional_config(self, cfg_item, field, item):
        """Set item config if present in themes config item.

        :param obj cfg_item: Themes config item
        :param str key: Config field
        :param obj item: Target theme item
        """
        if field in cfg_item:
            item[field] = cfg_item.get(field)

    def collect_layers(self, layer, search_layers):
        """Recursively collect layer tree from capabilities.

        :param obj layer: Layer or group layer
        :param obj search_layers: Lookup for search layers
        """
        # NOTE: use ordered keys
        item_layer = OrderedDict()

        item_layer['name'] = layer['name']
        if 'title' in layer:
            item_layer['title'] = layer['title']

        if 'layers' in layer:
            # group layer
            sublayers = []
            for sublayer in layer['layers']:
                # recursively collect sub layer
                sublayers.append(self.collect_layers(sublayer, search_layers))

            item_layer['sublayers'] = sublayers

            # expanded
            item_layer['expanded'] = layer.get("expanded")

            # mutuallyExclusive
            item_layer["mutuallyExclusive"] = layer.get("mutuallyExclusive")

            # visible
            item_layer['visibility'] = layer['visible']
        else:
            # layer
            item_layer['visibility'] = layer['visible']
            item_layer['queryable'] = layer['queryable']
            if 'display_field' in layer:
                item_layer['displayField'] = layer.get('display_field')
            item_layer['opacity'] = layer['opacity']
            if 'bbox' in layer:
                item_layer['bbox'] = {
                    'crs': 'EPSG:4326',
                    'bounds': layer.get('bbox')
                }

            # min/max scale
            minScale = layer.get("minScale")
            maxScale = layer.get("maxScale")
            if minScale:
                item_layer["minScale"] = int(float(minScale))
            if maxScale:
                item_layer["maxScale"] = int(float(maxScale))

            # abstract
            if 'abstract' in layer:
                item_layer['abstract'] = layer.get('abstract')
            # keywords
            if 'keywords' in layer:
                item_layer['keywords'] = layer.get('keywords')

            # search
            if layer['name'] in search_layers:
                item_layer['searchterms'] = [search_layers.get(layer['name'])]

            # TODO: featureReport

        return item_layer

    def edit_config(self, map_name, cfg_item):
        """Collect edit config for a map from ConfigDB.

        :param str map_name: Map name (matches WMS and QGIS project)
        """
        # NOTE: use ordered keys
        edit_config = OrderedDict()

        Permission = self.config_models.model('permissions')
        Resource = self.config_models.model('resources')

        session = self.config_models.session()

        # find map resource
        query = session.query(Resource) \
            .filter(Resource.type == 'map') \
            .filter(Resource.name == map_name)
        map_id = None
        for map_obj in query.all():
            map_id = map_obj.id

        if map_id is None:
            # map not found
            return edit_config

        # query writable data permissions
        resource_types = [
            'data',
            'data_create', 'data_read', 'data_update', 'data_delete'
        ]
        datasets_query = session.query(Permission) \
            .join(Permission.resource) \
            .filter(Permission.write) \
            .filter(Resource.parent_id == map_obj.id) \
            .filter(Resource.type.in_(resource_types)) \
            .distinct(Resource.name) \
            .order_by(Resource.name)
        edit_datasets = [
            permission.resource.name for permission in datasets_query.all()
        ]

        session.close()

        if not edit_datasets:
            # no edit datasets for this map
            return edit_config

        qgs_reader = QGSReader(self.logger, self.qgis_projects_base_dir)
        self.logger.info("Reading '%s.qgs'" % map_name)
        if qgs_reader.read(map_name):
            # collect edit datasets
            for layer_name in qgs_reader.pg_layers():
                if layer_name not in edit_datasets:
                    # skip layers not in datasets
                    continue

                dataset_name = "%s.%s" % (map_name, layer_name)

                try:
                    # get layer metadata from QGIS project
                    meta = qgs_reader.layer_metadata(layer_name)
                    qgs_reader.lookup_attribute_data_types(meta)
                except Exception as e:
                    self.logger.error(
                        "Could not get metadata for edit dataset '%s':\n%s" %
                        (dataset_name, e)
                    )
                    continue

                # check geometry type
                if not 'geometry_type' in meta or meta['geometry_type'] not in self.EDIT_GEOM_TYPES:
                    table = (
                        "%s.%s" % (meta.get('schema'), meta.get('table_name'))
                    )
                    self.logger.warning(
                        "Unsupported geometry type '%s' for edit dataset '%s' "
                        "on table '%s'" %
                        (meta.get('geometry_type', None), dataset_name, table)
                    )
                    continue

                # NOTE: use ordered keys
                dataset = OrderedDict()
                dataset['layerName'] = layer_name
                dataset['editDataset'] = dataset_name

                # collect fields
                fields = []
                for attr in meta.get('attributes'):
                    field = meta['fields'].get(attr, {})
                    alias = field.get('alias', attr)
                    data_type = self.EDIT_FIELD_TYPES.get(
                        field.get('data_type'), 'text'
                    )

                    # NOTE: use ordered keys
                    edit_field = OrderedDict()
                    edit_field['id'] = attr
                    edit_field['name'] = alias
                    edit_field['type'] = data_type

                    if 'constraints' in field:
                        # add any constraints
                        edit_field['constraints'] = field['constraints']
                        if 'values' in field['constraints']:
                            edit_field['type'] = 'list'

                    fields.append(edit_field)

                dataset['fields'] = fields
                dataset['geomType'] = self.EDIT_GEOM_TYPES.get(
                    meta['geometry_type']
                )

                edit_config[layer_name] = dataset

        # Preserve manually specified edit configs
        if 'editConfig' in cfg_item:
            for layer_name in cfg_item['editConfig']:
                edit_config[layer_name] = cfg_item['editConfig'][layer_name]

        return edit_config

    def copy_index_html(self):
        """Copy index.html to tenant dir."""

        # copy index.html
        # additional service config
        cfg_generator_config = self.service_config.get('generator_config', {})
        cfg_qwc2_config = cfg_generator_config.get('qwc2_config', {})

        self.logger.info("Copying 'index.html' to tenant dir")
        try:
            # read index.html
            index_file = cfg_qwc2_config.get('qwc2_index_file', 'index.html')
            index_contents = None
            with open(index_file) as f:
                index_contents = f.read()

            # write to tenant dir
            target_path = os.path.join(self.tenant_path, 'index.html')
            with open(target_path, 'w') as f:
                f.write(index_contents)
        except Exception as e:
            self.logger.error("Could not copy QWC2 index.html:\n%s" % e)

    # permissions

    def permitted_background_layers(self, role):
        """Return permitted internal print layers for background layers from
        capabilities and ConfigDB.

        :param str role: Role name
        """
        background_layers = []

        # TODO: get permissions and restrictions from ConfigDB
        #       everything permitted to public role for now
        if role != 'public':
            return []

        # QWC2 themes config
        themes_config = self.capabilities_reader.themes_config
        themes_config_themes = themes_config.get('themes', {})

        for bg_layer in themes_config_themes.get('backgroundLayers', []):
            background_layers.append(bg_layer.get('name'))

        return background_layers

    def permitted_viewer_tasks(self, role, session):
        """Return permitted viewer tasks from ConfigDB.

        :param str role: Role name
        :param Session session: DB session
        """
        # collect role permissions from ConfigDB
        viewer_tasks = self.permitted_resources(
            'viewer_task', role, session
        ).keys()

        return sorted(list(viewer_tasks))

    def permitted_theme_info_links(self, role, session):
        """Return permitted theme info links from ConfigDB.

        :param str role: Role name
        :param Session session: DB session
        """
        # collect role permissions from ConfigDB
        theme_info_links = self.permitted_resources(
            'theme_info_link', role, session
        ).keys()

        return sorted(list(theme_info_links))

    def permitted_plugin_data_resources(self, role, session):
        """Return permitted plugin data resources from ConfigDB.

        NOTE: 'plugin_data' require explicit permissions,
              permissions for 'plugin' are disregarded

        :param str role: Role name
        :param Session session: DB session
        """
        plugin_permissions = []

        # collect role permissions from ConfigDB
        for plugin, plugin_data in self.permitted_resources(
            'plugin_data', role, session
        ).items():
            # add permitted plugin data resources grouped by plugin
            # NOTE: use ordered keys
            plugin_permission = OrderedDict()
            plugin_permission['name'] = plugin
            plugin_permission['resources'] = sorted(list(plugin_data.keys()))
            plugin_permissions.append(plugin_permission)

        # order by plugin name
        return sorted(
            plugin_permissions, key=lambda plugin: plugin.get('name')
        )
