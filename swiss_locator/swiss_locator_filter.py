# -*- coding: utf-8 -*-
"""
/***************************************************************************

                                 QgisLocator

                             -------------------
        begin                : 2018-05-03
        copyright            : (C) 2018 by Denis Rouzaud
        email                : denis@opengis.ch
        git sha              : $Format:%H$
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""


import json
import os
import re

from PyQt5.QtCore import Qt
from PyQt5.QtGui import QColor
from PyQt5.QtCore import QUrl, QUrlQuery
from PyQt5.QtWidgets import QDialog
from PyQt5.uic import loadUiType

from qgis.core import Qgis, QgsMessageLog, QgsLocatorFilter, QgsLocatorResult, QgsRectangle, \
    QgsCoordinateReferenceSystem, QgsCoordinateTransform, QgsProject, QgsGeometry, QgsWkbTypes
from qgis.gui import QgsRubberBand, QgsMapCanvas

from .qgissettingmanager.setting_dialog import SettingDialog, UpdateMode
from .network_access_manager import NetworkAccessManager, RequestsException, RequestsExceptionUserAbort
from .settings import Settings
from .swiss_locator_plugin import DEBUG

DialogUi, _ = loadUiType(os.path.join(os.path.dirname(__file__), 'ui/config.ui'))

AVAILABLE_CRS = ['2056', '21781']
AVAILABLE_LANGUAGES = {'German': 'de',
                       'SwissGerman': 'de',
                       'French': 'fr',
                       'Italian': 'it',
                       'Romansh': 'rm',
                       'English': 'en'}


class ConfigDialog(QDialog, DialogUi, SettingDialog):
    def __init__(self, parent=None):
        settings = Settings()
        QDialog.__init__(self, parent)
        SettingDialog.__init__(self, setting_manager=settings, mode=UpdateMode.DialogAccept)
        self.setupUi(self)
        self.lang.addItem(self.tr('use the application locale, defaults to English'), '')
        for key, val in AVAILABLE_LANGUAGES.items():
            self.lang.addItem(key, val)
        self.crs.addItem('CH 1903+ (EPSG:2056)', '2056')
        self.crs.addItem('CH 1903 (EPSG:21781)', '21781')
        self.settings = settings
        self.init_widgets()


class InvalidBox(Exception):
    pass


class SwissLocatorFilter(QgsLocatorFilter):

    USER_AGENT = b'Mozilla/5.0 QGIS Swiss MapGeoAdmin Locator Filter'

    def __init__(self,  locale_lang: str, map_canvas: QgsMapCanvas = None):
        super().__init__()
        self.rubber_band = None
        self.map_canvas = None
        self.settings = Settings()
        self.reply = None

        self.locale_lang = locale_lang
        lang = self.settings.value('lang')
        if not lang:
            if locale_lang in AVAILABLE_LANGUAGES:
                self.lang = AVAILABLE_LANGUAGES[locale_lang]
            else:
                self.lang = 'en'
        else:
            self.lang = lang

        if map_canvas is not None:
            self.map_canvas = map_canvas
            self.rubber_band = QgsRubberBand(map_canvas)
            self.rubber_band.setColor(QColor(255, 255, 50, 200))
            self.rubber_band.setIcon(self.rubber_band.ICON_CIRCLE)
            self.rubber_band.setIconSize(15)
            self.rubber_band.setWidth(4)
            self.rubber_band.setBrushStyle(Qt.NoBrush)

    def translate_group(self, group) -> str:
        if group == 'zipcode':
            return self.tr('ZIP code')
        if group == 'gg25':
            return self.tr('Municipal boundaries')
        if group == 'district':
            return self.tr('District')
        if group == 'kantone':
            return self.tr('Cantons')
        if group == 'gazetteer':
            return self.tr('Index')
        if group == 'address':
            return self.tr('Address')
        if group == 'parcel':
            return self.tr('Parcel')
        raise NameError('Could not find group {} in dictionary'.format(group))

    @staticmethod
    def rank2priority(rank) -> float:
        """
        Translate the rank from GeoAdmin to the priority of the result
        see https://api3.geo.admin.ch/services/sdiservices.html#search
        :param rank: an integer from 1 to 7
        :return: the priority as a float from 0 to 1, 1 being a perfect match
        """
        return float(-rank / 7 + 1)

    @staticmethod
    def box2geometry(box: str) -> QgsRectangle:
        """
        Creates a rectangle from a Box definition as string
        :param box: the box as a string
        :return: the rectangle
        """
        coords = re.findall(r'\b(\d+(?:\.\d+)?)\b', box)
        if len(coords) != 4:
            raise InvalidBox('Could not parse: {}'.format(box))
        return QgsRectangle(float(coords[0]), float(coords[1]), float(coords[2]), float(coords[3]))

    def name(self):
        return self.__class__.__name__

    def clone(self):
        return SwissLocatorFilter(self.locale_lang)

    def displayName(self):
        return self.tr('Swiss Geoadmin locations')

    def prefix(self):
        return 'swi'

    def hasConfigWidget(self):
        return True

    def openConfigWidget(self, parent=None):
        ConfigDialog(parent).exec_()

    @staticmethod
    def url_with_param(url, params) -> str:
        url = QUrl(url)
        q = QUrlQuery(url)
        for key, value in params.items():
            q.addQueryItem(key, value)
        url.setQuery(q)
        return url.url()

    def fetchResults(self, search, context, feedback):
        self.dbg_info("start Swiss locator search...")

        if len(search) < 2:
            return

        if self.reply is not None and self.reply.isRunning():
            self.reply.abort()

        url = 'https://api3.geo.admin.ch/rest/services/api/SearchServer'
        params = {
            'type': 'locations',
            'searchText': str(search),
            'returnGeometry': 'true',
            'lang': self.lang,
            'sr': self.settings.value('crs')
        }
        #bbox Must be provided if the searchText is not. A comma separated list of 4 coordinates representing the bounding box on which features should be filtered (SRID: 21781).

        headers = {b'User-Agent': self.USER_AGENT}
        url = self.url_with_param(url, params)
        self.dbg_info(url)

        nam = NetworkAccessManager()
        feedback.canceled.connect(nam.abort)
        try:
            (response, content) = nam.request(url, headers=headers, blocking=True)
            self.handle_response(response, content)
        except RequestsExceptionUserAbort:
            pass
        except RequestsException as err:
            self.info(err)

    def handle_response(self, response, content):
        if response.status_code != 200:
            self.info("Error with status code: {}".format(response.status_code))
            return

        data = json.loads(content.decode('utf-8'))
        # self.dbg_info(data)

        for loc in data['results']:
            self.dbg_info("keys: {}".format(loc['attrs'].keys()))
            self.dbg_info("label: {}".format(loc['attrs']['label']))
            self.dbg_info("detail: {}".format(loc['attrs']['detail']))
            self.dbg_info("priority: {} (rank: {})".format(self.rank2priority(loc['attrs']['rank']), loc['attrs']['rank']))
            self.dbg_info("category: {} ({})".format(self.translate_group(loc['attrs']['origin']), loc['attrs']['origin']))
            self.dbg_info("bbox: {}".format(loc['attrs']['geom_st_box2d']))

            result = QgsLocatorResult()
            result.filter = self
            result.displayString = loc['attrs']['label']
            # result.description = loc['attrs']['detail']
            result.group = self.translate_group(loc['attrs']['origin'])
            result.userData = self.box2geometry(loc['attrs']['geom_st_box2d'])
            self.resultFetched.emit(result)
        return

    def triggerResult(self, result):
        # this should be run in the main thread, i.e. mapCanvas should not be None
        geometry = QgsGeometry.fromRect(result.userData)
        if not geometry:
            return

        srv_crs_authid = self.settings.value('crs')
        assert srv_crs_authid in AVAILABLE_CRS
        src_crs = QgsCoordinateReferenceSystem('EPSG:{}'.format(srv_crs_authid))
        assert src_crs.isValid()
        dst_crs = self.map_canvas.mapSettings().destinationCrs()
        tr = QgsCoordinateTransform(src_crs, dst_crs, QgsProject.instance())
        geometry.transform(tr)

        self.rubber_band.reset(geometry.type())
        self.rubber_band.addGeometry(geometry, None)
        rect = geometry.boundingBox()
        rect.scale(1.5)
        self.map_canvas.setExtent(rect)
        self.map_canvas.refresh()

    def beautify_group(self, group):
        if self.settings.value("remove_leading_digits"):
            group = re.sub('^\d+', '', group)
        if self.settings.value("replace_underscore"):
            group = group.replace("_", " ")
        if self.settings.value("break_camelcase"):
            group = self.break_camelcase(group)
        return group

    def info(self, msg="", level=Qgis.Info):
        QgsMessageLog.logMessage('{} {}'.format(self.__class__.__name__, msg), 'QgsLocatorFilter', level)

    def dbg_info(self, msg=""):
        if DEBUG:
            self.info(msg)

    @staticmethod
    def break_camelcase(identifier):
        matches = re.finditer('.+?(?:(?<=[a-z])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])|$)', identifier)
        return ' '.join([m.group(0) for m in matches])
