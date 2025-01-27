# coding=utf-8
from __future__ import absolute_import
import logging
import threading
import random
import string
import socket
import json
from datetime import datetime

import flask
import requests

import octoprint.plugin

from .localauth import LocalAuth
from .snapshothelper import SnapshotHelper
from .octoeverywhereimpl import OctoEverywhere
from .octohttprequest import OctoHttpRequest
from .notificationshandler import NotificationsHandler
from .octopingpong import OctoPingPong
from .slipstream import Slipstream

class OctoeverywherePlugin(octoprint.plugin.StartupPlugin,
                            octoprint.plugin.SettingsPlugin,
                            octoprint.plugin.AssetPlugin,
                            octoprint.plugin.TemplatePlugin,
                            octoprint.plugin.WizardPlugin,
                            octoprint.plugin.SimpleApiPlugin,
                            octoprint.plugin.EventHandlerPlugin,
                            octoprint.plugin.ProgressPlugin):

    def __init__(self):
        # The port this octoprint instance is listening on.
        self.OctoPrintLocalPort = 80
        # Default the handler to None since that will make the var name exist
        # but we can't actually create the class yet until the system is more initalized.
        self.NotificationHandler = None
        # Init member vars
        self.octoKey = ""

    # Assets we use, just for the wizard right now.
    def get_assets(self):
        return {
            "js"  : ["js/OctoEverywhere.js"],
            "less": ["less/OctoEverywhere.less"],
            "css" : ["css/OctoEverywhere.css"]
        }

    # Return true if the wizard needs to be shown.
    def is_wizard_required(self):
        # We don't need to show the wizard if we know there are account connected.
        hasConnectedAccounts = self.GetHasConnectedAccounts()
        return hasConnectedAccounts is False

    # Increment this if we need to pop the wizard again.
    def get_wizard_version(self):
        return 10

    def get_wizard_details(self):
        # Do some sanity checking logic, since this has been sensitive in the past.
        printerUrl = self.GetAddPrinterUrl()
        if printerUrl is None:
            self._logger.error("Failed to get OctoPrinter Url for wizard.")
            printerUrl = "https://t.me/printassistdemobot"
        return {"AddPrinterUrl": printerUrl}

    # Return the default settings.
    def get_settings_defaults(self):
        return dict(PrinterKey="", AddPrinterUrl="")

    # Return the current printer key for the settings template
    def get_template_vars(self):
        return dict(
            PrinterKey=self._settings.get(["PrinterKey"]),
            AddPrinterUrl=self.GetAddPrinterUrl()
        )

    def get_template_configs(self):
        return [
            dict(type="settings", custom_bindings=False)
        ]

    ##~~ Softwareupdate hook
    def get_update_information(self):
        # Define the configuration for your plugin to use with the Software Update
        # Plugin here. See https://docs.octoprint.org/en/master/bundledplugins/softwareupdate.html
        # for details.
        return dict(
            octoeverywhere=dict(
                displayName="octoeverywhere",
                displayVersion=self._plugin_version,

                # version check: github repository
                type="github_release",
                user="Florianja",
                repo="OctoPrint-OctoEverywhere-For-PrintAssist",
                current=self._plugin_version,

                # update method: pip
                pip="https://github.com/florianja/OctoPrint-OctoEverywhere-For-PrintAssist/archive/{target_version}.zip"
            )
        )

    # Called when the system is starting up.
    def on_startup(self, _, port):
        # Get the port the server is listening on, since for some configs it's not the default.
        self.OctoPrintLocalPort = port
        self._logger.info("OctoPrint port " + str(self.OctoPrintLocalPort))

        # Ensure they keys are created here, so make sure that they are always created before any of the UI queries for them.
        printerId = self.EnsureAndGetPrinterId()
        self.EnsureAndGetPrivateKey()

        # Ensure the plugin version is updated in the settings for the frontend.
        self.EnsurePluginVersionSet()

        # Init the static local auth helper
        LocalAuth.Init(self._logger, self._user_manager)

        # Init the static snapshot helper
        SnapshotHelper.Init(self._logger, self._settings)

        # Init the ping helper
        OctoPingPong.Init(self._logger, self.get_plugin_data_folder())

        # Create the notification object now that we have the logger.
        self.NotificationHandler = NotificationsHandler(self._logger, self._printer)
        self.NotificationHandler.SetPrinterId(printerId)

        # Spin off a thread to try to resolve hostnames for logging and debugging.
        resolverThread = threading.Thread(target=self.TryToPrintHostNameIps)
        resolverThread.start()


    # Call when the system is ready and running
    def on_after_startup(self):
        # Spin off a thread for us to operate on.
        self._logger.info("After startup called. Starting worker thread.")
        main_thread = threading.Thread(target=self.main)
        main_thread.daemon = True
        main_thread.start()

        # Init slipstream - This must be inited after LocalAuth since it requires the auth key.
        # Is also must be done when the OctoPrint server is ready, since it's going to kick off a thread to
        # pull and cache the index.
        Slipstream.Init(self._logger)

    #
    # Functions for the Simple API Mixin
    #
    def get_api_commands(self):
        return dict(
            # Our frontend js logic calls this API when it detects a local LAN connection and reports the port used.
            # We use the port internally as a solid indicator for what port the http proxy in front of OctoPrint is on.
            # This is required because it's common to also have webcams setup behind the http proxy and there's no other
            # way to query the port value from the system.
            setFrontendLocalPort=["port"]
        )

    def on_api_command(self, command, data):
        if command == "setFrontendLocalPort":
            # Ensure we can find a port.
            if "port" in data and data["port"] is not None:

                # Get vars
                port = int(data["port"])
                url = "Unknown"
                if "url" in data and data["url"] is not None:
                    url = str(data["url"])
                isHttps = False
                if "isHttps" in data and data["isHttps"] is not None:
                    isHttps = data["isHttps"]

                # Report
                self._logger.info("SetFrontendLocalPort API called. Port:"+str(port)+" IsHttps:"+str(isHttps)+" URL:"+url)
                # Save
                self._settings.set(["HttpFrontendPort"], port, force=True)
                self._settings.set(["HttpFrontendIsHttps"], isHttps, force=True)
                self._settings.save(force=True)
                # Update the running value.
                OctoHttpRequest.SetLocalHttpProxyPort(port)
                OctoHttpRequest.SetLocalHttpProxyIsHttps(isHttps)
            else:
                self._logger.info("SetFrontendLocalPort API called with no port.")
        else:
            self._logger.info("Unknown API command. "+command)


    def on_api_get(self, request):
        # On get requests, share some data.
        # This API is protected by the need for a OctoPrint API key
        # This API is used by apps and other system to identify the printer
        # for communication with the service. Thus these values should not be
        # modified or deleted.
        return flask.jsonify(
            PluginVersion=self._plugin_version,
            PrinterId=self.EnsureAndGetPrinterId()
        )

    #
    # Functions are for the gcode receive plugin hook
    #
    def received_gcode(self, comm, line, *args, **kwargs):
        # Blocking will block the printer commands from being handled so we can't block here!

        if line and self.NotificationHandler is not None:
            # ToLower the line for better detection.
            lineLower = line.lower()

            # M600 is a filament change command.
            # https://marlinfw.org/docs/gcode/M600.html
            # On my Pursa, I see this "fsensor_update - M600" AND this "echo:Enqueuing to the front: "M600""
            # We check for this both in sent and received, to make sure we cover all use cases. The OnFilamentChange will only allow one notification to fire every so often.
            # This m600 usually comes from when the printer sensor has detected a filament run out.
            if "m600" in lineLower or "fsensor_update" in lineLower:
                self._logger.info("Fireing On Filament Change Notification From GcodeReceived: "+str(line))
                # No need to use a thread since all events are handled on a new thread.
                self.NotificationHandler.OnFilamentChange()
            else:
                # Look for a line indicating user interaction is needed.
                if "paused for user" in lineLower or "// action:paused" in lineLower:
                    self._logger.info("Fireing On User Interaction Required From GcodeReceived: "+str(line))
                    # No need to use a thread since all events are handled on a new thread.
                    self.NotificationHandler.OnUserInteractionNeeded()

        # We must return line the line won't make it to OctoPrint!
        return line

    #
    # Functions are for the gcode sent plugin hook
    #
    def sent_gcode(self, comm_instance, phase, cmd, cmd_type, gcode, *args, **kwargs):
        # Blocking will block the printer commands from being handled so we can't block here!

        # M600 is a filament change command.
        # https://marlinfw.org/docs/gcode/M600.html
        # We check for this both in sent and received, to make sure we cover all use cases. The OnFilamentChange will only allow one notification to fire every so often.
        # This M600 usually comes from filament change required commands embedded in the gcode, for color changes and such.
        if self.NotificationHandler is not None and gcode and gcode == "M600":
            self._logger.info("Fireing On Filament Change Notification From GcodeSent: "+str(gcode))
            # No need to use a thread since all events are handled on a new thread.
            self.NotificationHandler.OnFilamentChange()

    #
    # Functions for the key validator hook.
    #
    def key_validator(self, api_key, *args, **kwargs):
        try:
            # Use LocalAuth to handle the request.
            return LocalAuth.Get().ValidateApiKey(api_key)
        except Exception as e:
            self._logger.error("key_validator failed "+str(e))
        return None

    #
    # Functions are for the Process Plugin
    #
    # pylint: disable=arguments-renamed
    def on_print_progress(self, storage, path, progressInt):
        if self.NotificationHandler is not None:
            self.NotificationHandler.OnPrintProgress(progressInt)

    #
    # Functions for the Event Handler Mixin
    #
    def on_event(self, event, payload):
        # Ensure there's a payload
        if payload is None:
            payload = {}

        # Listen for client authed events, these fire whenever a websocket opens and is auth is done.
        if event == "ClientAuthed":
            self.HandleClientAuthedEvent()

        # Only check the event after the notification handler has been created.
        # Specifically here, we have seen the Error event be fired before `on_startup` is fired,
        # and thus the handler isn't created.
        if self.NotificationHandler is None:
            return

        # Listen for the rest of these events for notifications.
        # OctoPrint Events
        if event == "PrintStarted":
            fileName = self.GetDictStringOrEmpty(payload, "name")
            self.NotificationHandler.OnStarted(fileName)
        elif event == "PrintFailed":
            fileName = self.GetDictStringOrEmpty(payload, "name")
            durationSec = self.GetDictStringOrEmpty(payload, "time")
            reason = self.GetDictStringOrEmpty(payload, "reason")
            self.NotificationHandler.OnFailed(fileName, durationSec, reason)
        elif event == "PrintDone":
            fileName = self.GetDictStringOrEmpty(payload, "name")
            durationSec = self.GetDictStringOrEmpty(payload, "time")
            self.NotificationHandler.OnDone(fileName, durationSec)
        elif event == "PrintPaused":
            fileName = self.GetDictStringOrEmpty(payload, "name")
            self.NotificationHandler.OnPaused(fileName)
        elif event == "PrintResumed":
            fileName = self.GetDictStringOrEmpty(payload, "name")
            self.NotificationHandler.OnResume(fileName)

        # Printer Connection
        elif event == "Error":
            error = self.GetDictStringOrEmpty(payload, "error")
            self.NotificationHandler.OnError(error)

        # GCODE Events
        # Note most of these aren't sent when printing from the SD card
        elif event == "ZChange":
            self.NotificationHandler.OnZChange()
        elif event == "Waiting":
            self.NotificationHandler.OnWaiting()


    def GetDictStringOrEmpty(self, d, key):
        if d[key] is None:
            return ""
        return str(d[key])


    def HandleClientAuthedEvent(self):
        # When the user is authed (opens the webpage in a new tab) we want to check if we should show the
        # finish setup message. This helps users setup the plugin if the miss the wizard or something.
        self.ShowLinkAccountMessageIfNeeded()

        # Check if an update is required, if so, tell the user everytime they login.
        pluginUpdateRequired = self.GetPluginUpdateRequired()
        if pluginUpdateRequired is True:
            title = "OctoEverywhere Disabled"
            message = '<br/><strong>You need to update your OctoEverywhere plugin before you can continue using OctoEverywhere.</strong><br/><br/>We are always improving OctoEverywhere to make things faster and add features. Sometimes, that means we have to break things. If you need info about how to update your plugin, <a target="_blank" href="https://octoeverywhere.com/pluginupdate">check this out.</i></a>'
            self.ShowUiPopup(title, message, "notice", True)


    def ShowLinkAccountMessageIfNeeded(self):
        addPrinterUrl = self.GetAddPrinterUrl()
        hasConnectedAccounts = self.GetHasConnectedAccounts()
        lastInformTimeDateTime = self.GetNoAccountConnectedLastInformDateTime()
        # Check if we know there are connected accounts or not, if we have a add printer URL, and finally if there are no accounts setup yet.
        # If we don't know about connected accounts or have a printer URL, we will skip this until we know for sure.
        if hasConnectedAccounts is False and addPrinterUrl is not None:
            # We will show a popup to help the user setup the plugin every little while. I have gotten a lot of feedback from support
            # tickets indicating this is a problem, so this might help it.
            #
            # We don't want to show the message the first time we load, since the wizard should show. After that we will show it some what frequently.
            # Ideally the user will either setup the plugin or remove it so it doesn't consume server resources.
            minTimeBetweenInformsSec = 60 * 1 # Every 1 mintue

            # Check the time since the last message.
            if lastInformTimeDateTime is None or (datetime.now() - lastInformTimeDateTime).total_seconds() > minTimeBetweenInformsSec:
                # Update the last show time.
                self.SetNoAccountConnectedLastInformDateTime(datetime.now())

                # Send the UI message.
                if lastInformTimeDateTime is None:
                    # Since the wizard is working now, we will skip the first time we detect this.
                    pass
                else:
                    # We want to show the finish setup message, but we only want to show it if the account is still unlinked.
                    # So we will kick off a new thread to make a http request to check before we show it.
                    t = threading.Thread(target=self.CheckIfPrinterIsSetupAndShowMessageIfNot)
                    t.start()


    # Should be called on a non-main thread!
    # Make a http request to ensure this printer is not owned and shows a pop-up to help the user finish the install if not.
    # TODO: change for PrintAssist
    def CheckIfPrinterIsSetupAndShowMessageIfNot(self):
        try:
            # Check if this printer is owned or not.
            # TODO: change for PrintAssist
            response = requests.post('https://octoeverywhere.com/api/printer/info', json={ "Id": self.EnsureAndGetPrinterId() })
            if response.status_code != 200:
                raise Exception("Invalid status code "+str(response.status_code))

            # Parse
            jsonData = response.json()
            hasOwners = jsonData["Result"]["HasOwners"]
            self._logger.info("Printer has owner: "+str(hasOwners))

            # If we are owned, update our settings and return!
            if hasOwners is True:
                self.SetHasConnectedAccounts(True)
                return

            # Ensure the printer URL - Add our source tag to it.
            addPrinterUrl = self.GetAddPrinterUrl()
            if addPrinterUrl is None:
                return
            addPrinterUrl += "&source=plugin_popup"

            # If not, show the message.
            title = "Complete Your Setup"
            message = '<br/>You\'re <strong>only 15 seconds</strong> away from OctoEverywhere\'s free remote access to OctoPrint from anywhere!<br/><br/><a class="btn btn-primary" style="color:white" target="_blank" href="'+addPrinterUrl+'">Finish Your Setup Now!&nbsp;&nbsp;<i class="fa fa-external-link"></i></a>'
            self.ShowUiPopup(title, message, "notice", True)

        except Exception as e:
            self._logger.error("CheckIfPrinterIsSetupAndShowMessageIfNot failed "+str(e))


    # The length the printer ID should be.
    # Note that the max length for a subdomain part (strings between . ) is 63 chars!
    # Making this a max of 60 chars allows for the service to use 3 chars prefixes for inter-service calls.
    c_OctoEverywherePrinterIdIdealLength = 60
    c_OctoEverywherePrinterIdMinLength = 40
    c_OctoEverywherePrivateKeyLength = 80

    # The url for the add printer process. Note this must have at least one ? and arg because users of it might append &source=blah
    # TODO: change for PrintAssist
    #c_OctoEverywhereAddPrinterUrl = "https://octoeverywhere.com/getstarted?isFromOctoPrint=true&printerid="

    c_OctoEverywhereAddPrinterUrl = "https://t.me/printassistdemobot?start="

    # Returns a new printer Id. This needs to be crypo-random to make sure it's not predictable.
    def GeneratePrinterId(self):
        return ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.digits) for _ in range(self.c_OctoEverywherePrinterIdIdealLength))

    # Returns a new private key. This needs to be crypo-random to make sure it's not predictable.
    def GeneratePrivateKey(self):
        return ''.join(random.SystemRandom().choice(string.ascii_uppercase + string.ascii_lowercase + string.digits) for _ in range(self.c_OctoEverywherePrivateKeyLength))

    # Ensures we have generated a printer id and returns it.
    def EnsureAndGetPrinterId(self):
        # Try to get the current.
        # "PrinterKey" is used by name in the static plugin JS and needs to be updated if this ever changes.
        currentId = self._settings.get(["PrinterKey"])

        # Make sure the current ID is valid.
        if currentId is None or len(currentId) < self.c_OctoEverywherePrinterIdMinLength:
            if currentId is None:
                self._logger.info("No printer id found, regenerating.")
            else:
                self._logger.info("Old printer id of length " + str(len(currentId)) + " is invlaid, regenerating.")
            # Create and save the new value
            currentId = self.GeneratePrinterId()
            self._logger.info("New printer id is: "+currentId)

        # Always update the settings, so they are always correct.
        self.SetAddPrinterUrl(self.c_OctoEverywhereAddPrinterUrl + currentId)
        # "PrinterKey" is used by name in the static plugin JS and needs to be updated if this ever changes.
        self._settings.set(["PrinterKey"], currentId, force=True)
        self._settings.save(force=True)
        return currentId

    # Ensures we have generated a private key and returns it.
    # This key not a key used for crypo purposes, but instead generated and tied to this instance's printer id.
    # The Printer id is used to ID the printer around the website, so it's more well known. This key is stored by this plugin
    # and is only used during the handshake to send to the server. Once set it can never be changed, or the server will reject the
    # handshake for the given printer ID.
    def EnsureAndGetPrivateKey(self):
        # Try to get the current.
        currentKey = self._settings.get(["Pid"])

        # Make sure the current ID is valid.
        if currentKey is None or len(currentKey) < self.c_OctoEverywherePrivateKeyLength:
            if currentKey is None:
                self._logger.info("No private key found, regenerating.")
            else:
                self._logger.info("Old private key of length " + str(len(currentKey)) + " is invlaid, regenerating.")

            # Create and save the new value
            currentKey = self.GeneratePrivateKey()

        # Save and return.
        self._settings.set(["Pid"], currentKey, force=True)
        self._settings.save(force=True)
        return currentKey

    # Ensures the plugin version is set into the settings for the frontend.
    def EnsurePluginVersionSet(self):
        # We save the current plugin version into the settings so the frontend JS can get it.
        self._settings.set(["PluginVersion"], self._plugin_version, force=True)
        self._settings.save(force=True)

    # Returns the frontend http port OctoPrint's http proxy is running on.
    def GetFrontendHttpPort(self):
        # Always try to get and parse the settings value. If the value doesn't exist
        # or it's invalid this will fall back to the default value.
        try:
            return int(self._settings.get(["HttpFrontendPort"]))
        except Exception:
            return 80

    # Returns the if the frontend http proxy for OctoPrint is using https.
    def GetFrontendIsHttps(self):
        # Always try to get and parse the settings value. If the value doesn't exist
        # or it's invalid this will fall back to the default value.
        try:
            return self._settings.get(["HttpFrontendIsHttps"])
        except Exception:
            return False

    # Sends a UI popup message for various uses.
    # title - string, the title text.
    # text  - string, the message.
    # type  - string, [notice, info, success, error] the type of message shown.
    # audioHide - bool, indicates if the message should auto hide.
    def ShowUiPopup(self, title, text, msgType, autoHide):
        data = {"title": title, "text": text, "type": msgType, "autoHide": autoHide}
        self._plugin_manager.send_plugin_message("octoeverywhere_ui_popup_msg", data)

    # Fired when the connection to the primary server is established.
    # connectedAccounts - a string list of connected accounts, can be an empty list.
    def OnPrimaryConnectionEstablished(self, octoKey, connectedAccounts):
        # On connection, set if there are connected accounts. We don't want to save the email
        # addresses in the settings, since they can be read by anyone that has access to the config
        # file or any plugin.
        hasConnectedAccounts = connectedAccounts is not None and len(connectedAccounts) > 0
        self.SetHasConnectedAccounts(hasConnectedAccounts)

        # Clear out the update required flag, since we connected.
        self.SetPluginUpdateRequired(False)

        # Always set the OctoKey as well.
        self.SetOctoKey(octoKey)

        # Clear this old value.
        self._settings.set(["ConnectedAccounts"], "", force=True)

        # Save
        self._settings.save(force=True)

    # Fired when the plugin needs to be updated before OctoEverywhere can be used again.
    # This should so a message to the user, so they know they need to update.
    def OnPluginUpdateRequired(self):
        self._logger.error("The OctoEverywhere service told us we must update before we can connect.")
        self.SetPluginUpdateRequired(True)
        self._settings.save(force=True)

    # Our main worker
    def main(self):
        self._logger.info("Main thread starting")

        try:
            # Get or create a printer id.
            printerId = self.EnsureAndGetPrinterId()
            privateKey = self.EnsureAndGetPrivateKey()

            # Get the frontend http port OctoPrint or it's proxy is running on.
            # This is the port the user would use if they were accessing OctoPrint locally.
            # Normally this is port 80, but some users might configure it differently.
            frontendHttpPort = self.GetFrontendHttpPort()
            frontendIsHttps = self.GetFrontendIsHttps()
            self._logger.info("Frontend http port detected as " + str(frontendHttpPort) + ", is https? "+str(frontendIsHttps))

            # Set the ports this instance is running on
            OctoHttpRequest.SetLocalHttpProxyPort(frontendHttpPort)
            OctoHttpRequest.SetLocalOctoPrintPort(self.OctoPrintLocalPort)
            OctoHttpRequest.SetLocalHttpProxyIsHttps(frontendIsHttps)

            # Run!
            # TODO: change for PrintAssist
            OctoEverywhereWsUri = "ws://printassist.local:7265/ws"
            oe = OctoEverywhere(OctoEverywhereWsUri, printerId, privateKey, self._logger, self, self, self._plugin_version)
            oe.RunBlocking()
        except Exception as e:
            self._logger.error("Exception thrown out of main runner. "+str(e))

    # For logging and debugging purposes, print the IPs the hostname is resolving to.
    # TODO: change for PrintAssist
    def TryToPrintHostNameIps(self):
        try:
            try:
                starportIp = socket.getaddrinfo('printassist.local', None, socket.AF_INET)[0][4][0]
                mainSiteIp = socket.getaddrinfo('printassist.local', None, socket.AF_INET)[0][4][0]
                self._logger.info("IPV4 - starport:"+str(starportIp)+" main:"+str(mainSiteIp))
            except Exception as e:
                self._logger.info("Failed to resolve host ipv4 name "+str(e))
            try:
                starportIp = socket.getaddrinfo('starport-v1.octoeverywhere.com', None, socket.AF_INET6)[0][4][0]
                mainSiteIp = socket.getaddrinfo('octoeverywhere.com', None, socket.AF_INET6)[0][4][0]
                self._logger.info("IPV6 - starport:"+str(starportIp)+" main:"+str(mainSiteIp))
            except Exception as e:
                self._logger.info("Failed to resolve host ipv6 name "+str(e))
        except Exception as _:
            pass

    #
    # Variable getters and setters.
    #

    def SetOctoKey(self, key):
        # We don't save the OctoKey to settings, keep it in memory.
        self.octoKey = key
        # We also need to set it into the notification handler.
        if self.NotificationHandler is not None:
            self.NotificationHandler.SetOctoKey(key)

    def GetOctoKey(self):
        if self.octoKey is None:
            return ""
        return self.octoKey

    def GetHasConnectedAccounts(self):
        return self.GetBoolFromSettings("HasConnectedAccounts", False)

    def SetHasConnectedAccounts(self, hasConnectedAccounts):
        self._settings.set(["HasConnectedAccounts"], hasConnectedAccounts is True, force=True)

    def GetPluginUpdateRequired(self):
        return self.GetBoolFromSettings("PluginUpdateRequired", False)

    def SetPluginUpdateRequired(self, pluginUpdateRequired):
        self._settings.set(["PluginUpdateRequired"], pluginUpdateRequired is True, force=True)

    def GetNoAccountConnectedLastInformDateTime(self):
        return self.GetFromSettings("NoAccountConnectedLastInformDateTime", None)

    def SetNoAccountConnectedLastInformDateTime(self, dateTime):
        self._settings.set(["NoAccountConnectedLastInformDateTime"], dateTime, force=True)

    # Returns None if there is no url set.
    def GetAddPrinterUrl(self):
        return self.GetFromSettings("AddPrinterUrl", None)

    def SetAddPrinterUrl(self, url):
        self._settings.set(["AddPrinterUrl"], url, force=True)

    # Gets the current setting or the default value.
    def GetBoolFromSettings(self, name, default):
        value = self._settings.get([name])
        if value is None:
            return default
        return value is True

    # Gets the current setting or the default value.
    def GetFromSettings(self, name, default):
        value = self._settings.get([name])
        if value is None:
            return default
        return value

__plugin_name__ = "OctoEverywhere for PrintAssist"
__plugin_pythoncompat__ = ">=2.7,<4" # py 2.7 or 3

def __plugin_load__():
    global __plugin_implementation__
    __plugin_implementation__ = OctoeverywherePlugin()

    global __plugin_hooks__
    __plugin_hooks__ = {
        "octoprint.accesscontrol.keyvalidator": __plugin_implementation__.key_validator,
        "octoprint.plugin.softwareupdate.check_config": __plugin_implementation__.get_update_information,
        "octoprint.comm.protocol.gcode.received": __plugin_implementation__.received_gcode,
        "octoprint.comm.protocol.gcode.sent": __plugin_implementation__.sent_gcode,
    }
