"""Support for HiSense TVs."""
import asyncio
from functools import partial
import json
import logging
import socket
import time
from typing import Callable, Dict, List, Optional

import paho.mqtt.client as mqtt
import voluptuous as vol
import wakeonlan

from homeassistant.components.media_player import PLATFORM_SCHEMA, MediaPlayerDevice
from homeassistant.components.media_player.const import (
    MEDIA_TYPE_APP,
    MEDIA_TYPE_CHANNEL,
    SUPPORT_PAUSE,
    SUPPORT_PLAY,
    SUPPORT_SELECT_SOURCE,
    SUPPORT_TURN_OFF,
    SUPPORT_TURN_ON,
    SUPPORT_VOLUME_STEP,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_MAC,
    CONF_NAME,
    CONF_PORT,
    EVENT_HOMEASSISTANT_STOP,
    STATE_OFF,
    STATE_PLAYING,
)
from homeassistant.core import Event, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import HomeAssistantType

from .const import DATA_DESCRIPTION, DEFAULT_NAME, DEFAULT_PORT, DOMAIN

# from homeassistant.util import dt as dt_util


_LOGGER = logging.getLogger(__name__)

SUPPORT_HISENSE = (
    SUPPORT_PAUSE
    | SUPPORT_VOLUME_STEP
    | SUPPORT_TURN_OFF
    | SUPPORT_SELECT_SOURCE
    | SUPPORT_PLAY
)

SUPPORT_HISENSE_MAC = SUPPORT_HISENSE | SUPPORT_TURN_ON

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Optional(CONF_NAME, default=DEFAULT_NAME): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): cv.port,
        vol.Optional(CONF_MAC): cv.port,
    }
)

MAX_RECONNECT_WAIT = 300  # seconds

CONNECTION_SUCCESS = "connection_success"
CONNECTION_FAILED = "connection_failed"
CONNECTION_FAILED_RECOVERABLE = "connection_failed_recoverable"

APP_KEYS = ["name", "urlType", "storeType", "url"]

STATE_INITIAL = 1
STATE_CONNECTING = 2
STATE_CONNECTED = 3
STATE_DISCONNECTED = 4
STATE_STANDBY = 5


async def async_setup_entry(
    hass: HomeAssistantType,
    entry: ConfigEntry,
    async_add_entities: Callable[[List, bool], None],
) -> bool:
    """Set up the HiSense TV config entry."""
    entities = []

    entity = HiSenseTvDevice(
        hass,
        entry.title,
        entry.data[CONF_HOST],
        entry.data[CONF_PORT],
        entry.data[CONF_MAC],
        entry.data[DATA_DESCRIPTION],
    )

    entities.append(entity)

    async def async_stop_mqtt(_event: Event):
        """Stop HiSense TV component."""
        await entity.async_disconnect()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_stop_mqtt)

    async_add_entities(entities, True)

    await entity.async_connect()


def _raise_on_error(result_code: int) -> None:
    """Raise error if error result."""

    if result_code != 0:
        raise HomeAssistantError(
            f"Error talking to HiSense TV: {mqtt.error_string(result_code)}"
        )


class HiSenseTvDevice(MediaPlayerDevice):
    """Representation of a HiSense TV on the network."""

    def __init__(
        self,
        hass: HomeAssistantType,
        name: str,
        host: str,
        port: int,
        mac: str,
        desc: Optional[Dict] = None,
        enabled_default: bool = True,
    ):
        """Initialize the device."""
        self.hass = hass
        client_id = "HomeAssistant"
        self._keepalive = 600
        self._name = name
        self._host = host
        self._mac = mac
        self._port = port
        self._is_standby = True
        self._volume = 0
        self._current = None
        self._last_update = None
        self._source_name_to_id = {}
        self._sources = []
        self._source = None
        self._apps = {}
        self._app_names = []
        self._paused = None
        self._assumed_state = None
        self._available = True
        self._enabled_default = enabled_default
        self._first_error_timestamp = None
        self._model = None
        self._receiver_id = None
        self._software_version = None
        self._topic_map = {
            "/remoteapp/mobile/broadcast/platform_service/actions/volumechange": self.on_volume,
            "/remoteapp/mobile/broadcast/ui_service/state": self.on_service_state,
            "/remoteapp/mobile/HomeAssistant/ui_service/data/sourcelist": self.on_sourcelist,
            "/remoteapp/mobile/AutoHTPC/ui_service/data/applist": self.on_applist,
            "/remoteapp/mobile/broadcast/platform_service/actions/tvsleep": self.on_sleep,
        }
        self._initial_publish_topics = [
            "/remoteapp/tv/platform_service/AutoHTPC/actions/getvolume",
            "/remoteapp/tv/ui_service/HomeAssistant/actions/sourcelist",
            "/remoteapp/tv/ui_service/AutoHTPC/actions/gettvstate",
            "/remoteapp/tv/ui_service/AutoHTPC/actions/applist",
        ]

        self._mqttc = mqtt.Client(client_id, protocol=mqtt.MQTTv311)
        self._mqttc.enable_logger(_LOGGER)
        self._mqttc.username_pw_set("hisenseservice", password="multimqttservice")
        self._paho_lock = asyncio.Lock()

        # The certificate is self-signed
        # self._mqttc.tls_set(cert_reqs=ssl.CERT_NONE)
        # self._mqttc.tls_insecure_set(True)

        self._mqttc.on_connect = self._mqtt_on_connect
        self._mqttc.on_disconnect = self._mqtt_on_disconnect
        self._mqttc.on_message = self._mqtt_on_message

        # if self._is_client:
        #     self._model = MODEL_CLIENT
        #     self._unique_id = device

        # if version_info:
        #     self._receiver_id = "".join(version_info["receiverId"].split())

        #     if not self._is_client:
        #         self._unique_id = self._receiver_id
        #         self._model = MODEL_HOST
        #         self._software_version = version_info["stbSoftwareVersion"]

    def on_service_state(self, str):
        """Parse state change message received via MQTT."""
        _LOGGER.info("state %s", str)
        state = json.loads(str)
        if state.get("statetype") == "sourceswitch":
            self._source = state.get("displayname")
            self._app = None
        elif state.get("statetype") == "app":
            self._app = state.get("url")
            self._source = None

    def on_sleep(self, str):
        """Set standby, TV is going to sleep."""
        self._is_standby = True
        self.hass.add_job(self.async_disconnect)

    def on_volume(self, str):
        """Parse volume change message."""
        cmd = json.loads(str)
        self._volume = cmd["volume_value"]

    def on_sourcelist(self, str):
        """Parse source list received via MQTT."""
        source_list = json.loads(str)
        sources = []
        source_name_to_id = {}
        for source in source_list:
            name = source["displayname"]
            sources.append(name)
            source_name_to_id[name] = source["sourceid"]
        sources.sort()
        self._sources = sources
        self._source_name_to_id = source_name_to_id

    def on_applist(self, str):
        """Parse app list received via MQTT."""
        app_list = json.loads(str)
        app_names = []
        apps = {}
        for app in app_list:
            minimal_app = {k: app[k] for k in APP_KEYS}
            app_names.append(minimal_app["name"])
            apps[minimal_app["name"]] = minimal_app
        self._app_names = app_names
        self._apps = apps

    async def async_connect(self) -> str:
        """Connect to the TV. Does process messages yet."""

        result: int = None
        try:
            result = await self.hass.async_add_executor_job(
                self._mqttc.connect, self._host, self._port, self._keepalive
            )
        except OSError as err:
            _LOGGER.error("Failed to connect due to exception: %s", err)
            return CONNECTION_FAILED_RECOVERABLE

        if result != 0:
            _LOGGER.error("Failed to connect: %s", mqtt.error_string(result))
            return CONNECTION_FAILED

        self._mqttc.loop_start()
        return CONNECTION_SUCCESS

    async def async_publish(self, topic: str, payload: str) -> None:
        """Publish a MQTT message."""
        async with self._paho_lock:
            _LOGGER.debug("Transmitting message on %s: %s", topic, payload)
            await self.hass.async_add_executor_job(
                self._mqttc.publish, topic, payload, 0, False
            )

    async def async_disconnect(self):
        """Stop the MQTT client."""

        def stop():
            """Stop the MQTT client."""
            self._mqttc.disconnect()
            self._mqttc.loop_stop()

        await self.hass.async_add_executor_job(stop)

    def _mqtt_on_connect(self, _mqttc, _userdata, _flags, result_code: int) -> None:
        """On connect callback.

        Subscribe to the wildcard topic so we get all updates
        """
        if result_code != mqtt.CONNACK_ACCEPTED:
            _LOGGER.error(
                "Unable to connect to the MQTT broker: %s",
                mqtt.connack_string(result_code),
            )
            self._mqttc.disconnect()
            return

        self._available = True
        self._is_standby = False
        self.hass.add_job(self._mqtt_subscribe)

    def _mqtt_on_disconnect(self, _mqttc, _userdata, result_code: int) -> None:
        """Disconnected callback."""
        self._is_standby = True

        # When disconnected because of calling disconnect()
        if result_code == 0:
            return

        tries = 0

        while True:
            try:
                if self._mqttc.reconnect() == 0:
                    _LOGGER.info(
                        "Successfully reconnected to the HiSense TV MQTT server"
                    )
                    break
            except socket.error:
                pass

            wait_time = min(2 ** tries, MAX_RECONNECT_WAIT)
            _LOGGER.debug(
                "Disconnected from HiSense TV MQTT (%s). Trying to reconnect in %s s",
                result_code,
                wait_time,
            )
            # It is ok to sleep here as we are in the MQTT thread.
            time.sleep(wait_time)
            tries += 1

    async def _mqtt_subscribe(self):
        """Subscribe to root topic and request initial topics."""
        _LOGGER.debug("Subscribing to root topic")

        async with self._paho_lock:
            result: int = None
            result, _ = await self.hass.async_add_executor_job(
                self._mqttc.subscribe, "#", 0
            )
            _raise_on_error(result)
            for topic in self._initial_publish_topics:
                await self.hass.async_add_executor_job(
                    self._mqttc.publish, topic, "0", 0, False
                )

    def _mqtt_on_message(self, _mqttc, _userdata, msg) -> None:
        """Message received callback."""
        self.hass.add_job(self._mqtt_handle_message, msg)

    @callback
    def _mqtt_handle_message(self, msg) -> None:
        callback = self._topic_map.get(msg.topic)
        _LOGGER.debug(
            "Received message on %s(%s): %s", msg.topic, callback, msg.payload,
        )

        if not callback:
            return

        self.hass.async_run_job(callback, msg.payload)

    def update(self):
        """Retrieve latest state."""
        _LOGGER.debug("%s: Updating status", self.entity_id)
        return self._available

    @property
    def device_state_attributes(self):
        """Return device specific state attributes."""
        attributes = {}
        # if not self._is_standby:
        # attributes[ATTR_MEDIA_CURRENTLY_RECORDING] = self.media_currently_recording
        # attributes[ATTR_MEDIA_RATING] = self.media_rating
        # attributes[ATTR_MEDIA_RECORDED] = self.media_recorded
        # attributes[ATTR_MEDIA_START_TIME] = self.media_start_time

        return attributes

    @property
    def name(self):
        """Return the name of the device."""
        return self._name

    @property
    def unique_id(self):
        """Return a unique ID to use for this media player."""
        return self._mac

    @property
    def device_info(self):
        """Return device specific attributes."""
        return {
            "name": self.name,
            "identifiers": {(DOMAIN, self.unique_id)},
            "manufacturer": "HiSense",
            "model": self._model,
            "sw_version": self._software_version,
            "via_device": (DOMAIN, self._receiver_id),
        }

    @property
    def entity_registry_enabled_default(self) -> bool:
        """Return if the entity should be enabled when first added to the entity registry."""
        return self._enabled_default

    # MediaPlayerDevice properties and methods
    @property
    def state(self):
        """Return the state of the device."""
        if self._is_standby:
            return STATE_OFF

        return STATE_PLAYING

    @property
    def media_content_type(self):
        """Content type of current playing media."""
        return MEDIA_TYPE_CHANNEL if self._source else MEDIA_TYPE_APP

    @property
    def available(self):
        """Return if able to retrieve information from DVR or not."""
        return self._available

    # @property
    # def assumed_state(self):
    #     """Return if we assume the state or not."""
    #     return self._assumed_state

    @property
    def source(self):
        """Name of the current input source."""
        if self._is_standby:
            return None

        return self._source or self._app

    @property
    def source_list(self):
        """List of available input sources."""
        return self._sources + self._app_names

    @property
    def supported_features(self):
        """Flag media player features that are supported."""
        return SUPPORT_HISENSE_MAC if self._mac else SUPPORT_HISENSE

    @property
    def volume_level(self):
        """Volume level of the media player (0..1)."""
        return self._volume / 100.0

    def __send_key(self, str):
        self.hass.add_job(
            self.async_publish,
            "/remoteapp/tv/remote_service/HomeAssistant/actions/sendkey",
            str,
        )

    def turn_on(self):
        """Turn on the receiver."""
        self.hass.add_job(partial(wakeonlan.send_magic_packet, self._mac))
        self.hass.add_job(self.async_connect)

    def turn_off(self):
        """Turn off the receiver."""
        self.__send_key("KEY_POWER")

    def media_play(self):
        """Send play command."""
        _LOGGER.debug("Play on %s", self._name)
        self.dtv.key_press("play")

    def media_pause(self):
        """Send pause command."""
        _LOGGER.debug("Pause on %s", self._name)
        self.dtv.key_press("pause")

    def media_stop(self):
        """Send stop command."""
        _LOGGER.debug("Stop on %s", self._name)
        self.dtv.key_press("stop")

    def media_previous_track(self):
        """Send rewind command."""
        _LOGGER.debug("Rewind on %s", self._name)
        self.dtv.key_press("rew")

    def media_next_track(self):
        """Send fast forward command."""
        _LOGGER.debug("Fast forward on %s", self._name)
        self.dtv.key_press("ffwd")
