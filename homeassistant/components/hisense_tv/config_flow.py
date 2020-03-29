"""Config flow for HiSense TV Control."""

import re
from typing import Any, Dict, Optional
from urllib.parse import urlparse

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.components.ssdp import ATTR_SSDP_LOCATION
from homeassistant.config_entries import CONN_CLASS_LOCAL_POLL
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_NAME, CONF_PORT
from homeassistant.core import callback
from homeassistant.helpers.typing import DiscoveryInfoType

from .const import DATA_DESCRIPTION, DEFAULT_PORT, DOMAIN, LOGGER

ERROR_CANNOT_CONNECT = "cannot_connect"
ERROR_UNKNOWN = "unknown"

DATA_SCHEMA = vol.Schema({vol.Required(CONF_HOST): str})
DESCRIPTION_PARSE_RE = re.compile(r"^([a-zA-Z_]+)=([^\n]+)$", re.MULTILINE)


"""
Location http://192.168.32.171:38400/MediaServer/rendererdevicedesc.xml
UDN uuid:cd931da7-dbd6-11e8-85d1-ab902b8c68fa
Type urn:schemas-upnp-org:device:MediaRenderer:1
Base URL http://192.168.32.171:38400/MediaServer/rendererdevicedesc.xml
Friendly Name Smart TV
Manufacturer
Manufacturer URL http://www.hisense.com/
Model Description #CAP#
mac=08ba5fc3fed7
macWifi=10c7535e0073
macEthernet=08ba5fc3fed7
ip=192.168.32.171
conf_version=1.0
region=4
country=CZE
model_name=HX58A6100UWTS
tv_version=V0000.01.00a.J0819
language=eng
transport_protocol=1
platform=1
remote_type=1
emanual=0
network_wakeup=1
input=1
voice=0
wire=0
cap=0
setname=1
mqttport=36669

Model Name Renderer
Model Number 1.0
Model URL http://www.hisense.com/
"""


class HiSenseFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a HiSense TV config flow."""

    VERSION = 1
    CONNECTION_CLASS = CONN_CLASS_LOCAL_POLL

    @callback
    def _show_form(self, errors: Optional[Dict] = None) -> Dict[str, Any]:
        """Show the form to the user."""
        return self.async_show_form(step_id="user", data_schema=DATA_SCHEMA, errors={},)

    async def async_step_ssdp(
        self, discovery_info: Optional[DiscoveryInfoType] = None
    ) -> Dict[str, Any]:
        """Handle a flow initialized by discovery."""
        desc = {}
        receiver_id = None
        raw_description = discovery_info.get("modelDescription")
        if not raw_description:
            return self.async_abort(reason=ERROR_UNKNOWN)

        for desc_match in re.finditer(DESCRIPTION_PARSE_RE, raw_description):
            key = desc_match.group(1)
            desc[key] = desc_match.group(2)

        host = urlparse(discovery_info[ATTR_SSDP_LOCATION]).hostname
        receiver_id = None
        mac = None

        if "mac" in desc:
            receiver_id = desc["mac"]
            mac = receiver_id
        elif "macWifi" in desc:
            receiver_id = desc["macWifi"]
        elif "macEthernet" in desc:
            receiver_id = desc["macEthernet"]

        name = host
        if "model_name" in desc:
            name = desc["model_name"]

        port = DEFAULT_PORT
        if "mqttport" in desc:
            port = int(desc["mqttport"])

        if receiver_id:
            await self.async_set_unique_id(receiver_id)
        self._abort_if_unique_id_configured(updates={CONF_HOST: host})

        # pylint: disable=no-member # https://github.com/PyCQA/pylint/issues/3167
        self.context.update(
            {
                CONF_HOST: host,
                CONF_NAME: name,
                CONF_PORT: port,
                CONF_MAC: mac,
                DATA_DESCRIPTION: desc,
                "title_placeholders": {"name": name},
            }
        )

        return await self.async_step_ssdp_confirm()

    async def async_step_ssdp_confirm(
        self, user_input: Optional[Dict] = None
    ) -> Dict[str, Any]:
        """Handle user-confirmation of discovered device."""
        # pylint: disable=no-member # https://github.com/PyCQA/pylint/issues/3167
        name = self.context.get(CONF_NAME)

        if user_input is not None:
            # pylint: disable=no-member # https://github.com/PyCQA/pylint/issues/3167
            user_input[CONF_HOST] = self.context.get(CONF_HOST)
            user_input[DATA_DESCRIPTION] = self.context.get(DATA_DESCRIPTION)
            if self.context.get(CONF_MAC) is not None:
                user_input[CONF_MAC] = self.context.get(CONF_MAC)
            if self.context.get(CONF_PORT) is not None:
                user_input[CONF_PORT] = self.context.get(CONF_PORT)

            try:
                return self.async_create_entry(title=name, data=user_input)
            except (OSError):
                return self.async_abort(reason=ERROR_CANNOT_CONNECT)
            except Exception:  # pylint: disable=broad-except
                LOGGER.exception("Unexpected exception")
                return self.async_abort(reason=ERROR_UNKNOWN)

        return self.async_show_form(
            step_id="ssdp_confirm", description_placeholders={"name": name},
        )

    # async def async_step_ssdp(self, discovery_info):
    #     """Handle a discovered deCONZ bridge."""
    #     if (
    #         discovery_info.get(ssdp.ATTR_UPNP_MANUFACTURER_URL)
    #         != DECONZ_MANUFACTURERURL
    #     ):
    #         return self.async_abort(reason="not_deconz_bridge")

    #     LOGGER.debug("deCONZ SSDP discovery %s", pformat(discovery_info))

    #     self.bridge_id = normalize_bridge_id(discovery_info[ssdp.ATTR_UPNP_SERIAL])
    #     parsed_url = urlparse(discovery_info[ssdp.ATTR_SSDP_LOCATION])

    #     entry = await self.async_set_unique_id(self.bridge_id)
    #     if entry and entry.source == "hassio":
    #         return self.async_abort(reason="already_configured")

    #     self._abort_if_unique_id_configured(
    #         updates={CONF_HOST: parsed_url.hostname, CONF_PORT: parsed_url.port}
    #     )

    #     # pylint: disable=no-member # https://github.com/PyCQA/pylint/issues/3167
    #     self.context["title_placeholders"] = {"host": parsed_url.hostname}

    #     self.deconz_config = {
    #         CONF_HOST: parsed_url.hostname,
    #         CONF_PORT: parsed_url.port,
    #     }

    #     return await self.async_step_link()
