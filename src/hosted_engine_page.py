#!/usr/bin/python
# -*- coding: utf-8 -*-
#
# hosted_engine_page.py - Copyright (C) 2014 Red Hat, Inc.
# Written by Joey Boggs <jboggs@redhat.com>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; version 2 of the License.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA  02110-1301, USA.  A copy of the GNU General Public License is
# also available at http://www.gnu.org/copyleft/gpl.html.

from urlparse import urlparse

from ovirt.node import plugins, ui, utils, valid
from ovirt.node.plugins import Changeset
from ovirt.node.config.defaults import NodeConfigFileSection
from ovirt.node.utils.fs import File
from ovirt_hosted_engine_ha.client import client

import json
import os
import requests
import tempfile
import threading
import time

"""
Configure Hosted Engine
"""


class Plugin(plugins.NodePlugin):
    VM_CONF_PATH = "/etc/ovirt-hosted-engine/vm.conf"
    HOSTED_ENGINE_SETUP_DIR = "/data/ovirt-hosted-engine-setup"
    _server = None
    _show_progressbar = False
    _model = {}

    def __init__(self, application):
        super(Plugin, self).__init__(application)

    def name(self):
        return "Hosted Engine"

    def rank(self):
        return 110

    def has_ui(self):
        is_el6 = utils.system.SystemRelease().is_el()
        has_override = HostedEngine().retrieve()["force_enable"]
        return is_el6 or has_override

    def update(self, imagepath):
        (valid.Empty() | valid.Text())(imagepath)
        return {"OVIRT_HOSTED_ENGINE_IMAGE_PATH": imagepath}

    def model(self):
        cfg = HostedEngine().retrieve()

        configured = os.path.exists(self.VM_CONF_PATH)

        conf_status = "Configured" if configured else "Not configured"
        vm = None
        if conf_status == "Configured":
            f = File(self.VM_CONF_PATH)
            if "vmName" in f.read():
                vm = [line.strip().split("=")[1] for line in f
                      if "vmName" in line][0]
            vm_status = self.__get_ha_status()
        else:
            vm_status = "Hosted engine not configured"

        model = {
            "hosted_engine.enabled": str(conf_status),
            "hosted_engine.vm": vm,
            "hosted_engine.status": vm_status,
            "hosted_engine.diskpath": cfg["imagepath"] or "",
            "hosted_engine.display_message": "",
            "hosted_engine.pxe": cfg["pxe"]}

        self._model.update(model)

        return self._model

    def validators(self):
        return {"hosted_engine.diskpath": valid.Empty() | valid.URL()}

    def ui_content(self):
        ws = [ui.Header("header[0]", "Hosted Engine Setup"),
              ui.KeywordLabel("hosted_engine.enabled",
                              ("Hosted Engine: ")),

              ui.Divider("divider[0]"),
              ui.KeywordLabel("hosted_engine.vm",
                              ("Engine VM Name: ")),
              ui.KeywordLabel("hosted_engine.status",
                              ("Engine Status: ")),

              ui.Divider("divider[0]"),
              ui.Entry("hosted_engine.diskpath",
                       "Engine ISO/OVA URL for download:"),
              ui.Divider("divider[1]"),
              ui.Checkbox("hosted_engine.pxe", "PXE Boot Engine VM")
              ]

        if self._show_progressbar:
            if "progress" in self._model:
                ws.append(ui.ProgressBar("download.progress",
                                         int(self._model["progress"])))
            else:
                ws.append(ui.ProgressBar("download.progress", 0))

            ws.append(ui.KeywordLabel("download.status", ""))

        page = ui.Page("page", ws)
        page.buttons = [ui.Button("action.setupengine",
                                  "Setup Hosted Engine")
                        ]
        self.widgets.add(page)
        return page

    def on_change(self, changes):
        pass

    def on_merge(self, effective_changes):
        self._install_ready = False
        changes = Changeset(self.pending_changes(False))
        effective_model = Changeset(self.model())
        effective_model.update(effective_changes)

        self.logger.debug("Changes: %s" % changes)
        self.logger.debug("Effective Model: %s" % effective_model)

        engine_keys = ["hosted_engine.diskpath", "hosted_engine.pxe"]

        if effective_changes.contains_any(["action.setupengine"]):
            HostedEngine().update(*effective_model.values_for(engine_keys))

            imagepath = effective_model["hosted_engine.diskpath"]
            pxe = effective_model["hosted_engine.pxe"]

            # Check whether we have unclear conditions
            if not imagepath and not pxe:
                self._model['display_message'] = "\n\nYou must enter a URL" \
                    " or choose PXE to install the Engine VM"
                self.show_dialog()
                return self.ui_content()
            elif imagepath and pxe:
                self._model['display_message'] = "\n\nPlease choose either " \
                                                 "PXE or an image to " \
                                                 "retrieve, not both"
                self.show_dialog()
                return self.ui_content()

            if not os.path.exists(self.HOSTED_ENGINE_SETUP_DIR):
                os.makedirs(self.HOSTED_ENGINE_SETUP_DIR)

            temp_fd, self.temp_cfg_file = tempfile.mkstemp()
            os.close(temp_fd)

            if pxe:
                self.write_config(pxe=True)
                self._install_ready = True
                self.show_dialog()

            else:
                localpath = os.path.join(self.HOSTED_ENGINE_SETUP_DIR,
                                         os.path.basename(imagepath))

                if os.path.exists(localpath):
                    # The image is already downloaded. Use that.
                    self.write_config(imagepath=os.path.basename(imagepath))

                    self._install_ready = True
                    self.show_dialog()

                else:
                    path_parsed = urlparse(imagepath)

                    if not path_parsed.scheme:
                        self._model['display_message'] = ("\nCouldn't parse "
                                                          "URL. please check "
                                                          "it manually.")

                    elif path_parsed.scheme == 'http' or \
                            path_parsed.scheme == 'https':
                        self._show_progressbar = True
                        self.application.show(self.ui_content())
                        self._image_retrieve(imagepath,
                                             self.HOSTED_ENGINE_SETUP_DIR)

        return self.ui_content()

    def show_dialog(self):
        def open_console():
            utils.process.call("reset; screen ovirt-hosted-engine-setup" +
                               " --config-append=%s" % self.temp_cfg_file,
                               shell=True)

        def return_ok(dialog, changes):
            with self.application.ui.suspended():
                open_console()

        if self.application.current_plugin() is self:
            try:
                # Clear out the counters once we're done, and hide the progress
                # bar
                self.widgets["download.progress"].current(0)
                self.widgets["download.status"].text("")
                self._show_progressbar = False

                self._model["download.progress"] = 0
                self._model["download.status"] = ""

                if self._install_ready:
                    utils.console.writeln("Beginning Hosted Engine Setup ...")
                    txt = "Setup will be ran with screen enabled that can be "
                    txt += "reconnected in the event of a timeout or "
                    txt += "connection failure.\n"
                    txt += "\nIt can be reconnected by running 'screen -d -r'"

                    dialog = ui.ConfirmationDialog("dialog.shell",
                                                   "Begin Hosted Engine Setup",
                                                   txt
                                                   )
                    dialog.buttons[0].on_activate.clear()
                    dialog.buttons[0].on_activate.connect(ui.CloseAction())
                    dialog.buttons[0].on_activate.connect(return_ok)
                else:
                    if self._model['display_message']:
                        msg = self._model['display_message']
                        self._model['display_message'] = ''
                    else:
                        msg = "\n\nError Downloading ISO/OVA Image!"

                    dialog = ui.InfoDialog("dialog.notice",
                                           "Hosted Engine Setup",
                                           msg)

                self.application.show(dialog)

            except:
                # Error when the UI is not running
                self.logger.info("Exception on TUI!", exc_info=True)
                open_console()

        self.application.show(self.ui_content())

    def _image_retrieve(self, imagepath, setup_dir):
        _downloader = DownloadThread(self, imagepath, setup_dir)
        _downloader.start()

    def magic_type(self, imagepath, type="gzip"):
        magic_headers = {"gzip": "\x1f\x8b\x08"}

        with open(imagepath) as f:
            magic = f.read(len(magic_headers[type]))

        return True if magic == magic_headers[type] else False

    def write_config(self, imagepath=None, pxe=False):
        f = File(self.temp_cfg_file)

        def write(line):
            f.write("{line}\n".format(line=line), "a")

        self.logger.info("Saving Hosted Engine Config")

        ova_path = None
        boot = None
        write("[environment:default]")

        if pxe:
            boot = "pxe"

        if imagepath:
            imagepath = os.path.join(self.HOSTED_ENGINE_SETUP_DIR,
                                     imagepath.lstrip("/"))
            if imagepath.endswith(".iso"):
                boot = "cdrom"
                write("OVEHOSTED_VM/vmCDRom=str:{imagepath}".format(
                    imagepath=imagepath))
            else:
                imagetype = "gzip" if self.magic_type(imagepath) else "Unknown"
                if imagetype == "gzip":
                    boot = "disk"
                    ova_path = imagepath
                else:
                    raise RuntimeError("Downloaded image is neither an OVA nor"
                                       " an ISO, can't use it")

        write("OVEHOSTED_VM/vmBoot=str:{boot}".format(boot=boot))

        ovastr = "str:{ova_path}".format(ova_path=ova_path) if ova_path else \
                 "none:None"
        write("OVEHOSTED_VM/ovfArchive={ovastr}".format(ovastr=ovastr))

        self.logger.info("Wrote hosted engine install configuration to "
                         "{cfg}".format(cfg=self.temp_cfg_file))
        self.logger.debug("Wrote config as:")
        for line in f:
            self.logger.debug("{line}".format(line=line.strip()))

    def __get_ha_status(self):
        def dict_from_string(string):
            return json.loads(string)

        host = None

        ha_cli = client.HAClient()
        try:
            vm_status = ha_cli.get_all_host_stats()
        except:
            vm_status = "Cannot connect to HA daemon, please check the logs"
            return vm_status
        else:
            for v in vm_status.values():
                if dict_from_string(v['engine-status'])['health'] == "good":
                    host = "Here" if v['host-id'] == \
                           ha_cli.get_local_host_id() else v['hostname']
                    host = v['hostname']

        if not host:
            vm_status = "Engine is down or not deployed."
        else:
            vm_status = "Engine is running on {host}".format(host=host)

        return vm_status


class DownloadThread(threading.Thread):
    ui_thread = None

    def __init__(self, plugin, url, setup_dir):
        super(DownloadThread, self).__init__()
        self.he_plugin = plugin
        self.url = url
        self.setup_dir = setup_dir

    @property
    def logger(self):
        return self.he_plugin.logger

    def run(self):
        try:
            self.app = self.he_plugin.application
            self.ui_thread = self.app.ui.thread_connection()

            self.__run()
        except Exception as e:
            self.logger.exception("Downloader thread failed: %s " % e)

    def __run(self):
        # Wait a second before the UI refresh so we get the right widgets
        time.sleep(.5)

        path = "%s/%s" % (self.setup_dir, self.url.split('/')[-1])

        ui_is_alive = lambda: any((t.name == "MainThread") and t.is_alive() for
                                  t in threading.enumerate())

        with open(path, 'wb') as f:
            started = time.time()
            try:
                r = requests.get(self.url, stream=True)
                if r.status_code != 200:
                    self.he_plugin._model['display_message'] = \
                        "\n\nCannot download the file: HTTP error code %s" % \
                        str(r.status_code)
                    os.unlink(path)
                    return self.he_plugin.show_dialog()

                size = r.headers.get('content-length')
            except requests.exceptions.ConnectionError as e:
                self.logger.info("Error downloading: %s" % e[0], exc_info=True)
                self.he_plugin._model['display_message'] = \
                    "\n\nConnection Error: %s!" % str(e[0])
                os.unlink(path)
                return self.he_plugin.show_dialog()

            downloaded = 0

            def update_ui():
                # Get new handles every time, since switching pages means
                # the widgets will get rebuilt and we need new handles to
                # update
                progressbar = self.he_plugin.widgets["download.progress"]
                status = self.he_plugin.widgets["download.status"]

                current = int(100.0 * (float(downloaded) / float(size)))

                progressbar.current(current)
                speed = calculate_speed()
                status.text(speed)

                # Save it in the model so the page can update immediately
                # on switching back instead of waiting for a tick
                self.he_plugin._model.update({"download.status": speed})
                self.he_plugin._model.update({"download.progressbar": current})

            def calculate_speed():
                raw = downloaded // (time.time() - started)
                i = 0
                friendly_names = ("B", "KB", "MB", "GB")
                if int(raw / 1024) > 0:
                    raw = raw / 1024
                    i += 1
                return "%0.2f %s/s" % (raw, friendly_names[i])

            for chunk in r.iter_content(1024 * 256):
                downloaded += len(chunk)
                f.write(chunk)

                if ui_is_alive():
                    self.ui_thread.call(update_ui())
                else:
                    break

        if not ui_is_alive():
            os.unlink(path)

        else:
            self.he_plugin.write_config(os.path.basename(path))
            self.he_plugin._install_ready = True
            self.he_plugin.show_dialog()


class HostedEngine(NodeConfigFileSection):
    keys = ("OVIRT_HOSTED_ENGINE_IMAGE_PATH",
            "OVIRT_HOSTED_ENGINE_PXE",
            "OVIRT_HOSTED_ENGINE_FORCE_ENABLE",
            )

    @NodeConfigFileSection.map_and_update_defaults_decorator
    def update(self, imagepath, pxe, force_enable=None):
        if not isinstance(pxe, bool):
            pxe = True if pxe.lower() == 'true' else False
        (valid.Empty() | valid.Text())(imagepath)
        (valid.Boolean()(pxe))
        return {"OVIRT_HOSTED_ENGINE_IMAGE_PATH": imagepath,
                "OVIRT_HOSTED_ENGINE_PXE": "yes" if pxe else None,
                "OVIRT_HOSTED_ENGINE_FORCE_ENABLE": "yes" if force_enable else None}

    def retrieve(self):
        cfg = dict(NodeConfigFileSection.retrieve(self))
        cfg.update({"pxe": True if cfg["pxe"] == "yes" else False})
        cfg.update({"force_enable": True if cfg["force_enable"] == "yes" else False})
        return cfg
