"""Platform for climate integration."""

from __future__ import annotations

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.helpers.restore_state import RestoreEntity

from .__init__ import NilanEntity
from .const import DOMAIN

HVAC_MODE_TO_STATE = {
    1: HVACMode.HEAT,
    2: HVACMode.COOL,
    3: HVACMode.AUTO,
}

STATE_TO_HVAC_MODE = {
    HVACMode.HEAT: 1,
    HVACMode.COOL: 2,
    HVACMode.AUTO: 3,
}

# Compensation de la sonde de régulation.
# La CTS602 régule sur T15, placée dans une boîte technique (+6-7 °C), et refuse
# le registre de choix de sonde (1208, bus v4). L'entité affiche donc T10 (air
# extrait = moyenne réelle des pièces) et accepte une consigne "réelle" ; on
# écrit à la carte : consigne réelle + (T_régulation - T10), arrondie au degré.
REAL_TEMP_STEP = 0.5
REAL_TEMP_MIN = 10
REAL_TEMP_MAX = 28
REWRITE_HYSTERESIS = 0.75  # évite de réécrire sans cesse quand l'écart oscille

PRESET_TO_HVAC = {
    "energy": 0,
    "comfort": 1,
    "water": 2,
}

HVAC_TO_PRESET = {
    0: "energy",
    1: "comfort",
    2: "water",
}


async def async_setup_entry(HomeAssistant, config_entry, async_add_entities):
    """Add climate entities for a config entry."""
    supported_features = (
        ClimateEntityFeature.FAN_MODE
        | ClimateEntityFeature.TURN_OFF
        | ClimateEntityFeature.TURN_ON
    )

    hvac_basic_attributes = [
        "get_run_state",
        "get_operation_mode",
        "get_ventilation_step",
        "get_control_state",
    ]
    hvac_extra_status_attributes = [
        "get_ventilation_state",
        "get_supply_fan_level",
    ]
    hvac_temperature_attributes = [
        "get_user_temperature_setpoint",
        "get_control_temperature",
    ]

    hvac_preset_attributes = [
        "get_air_exchange_mode",
    ]

    hvac_humidity_attributes = [
        "get_user_humidity_setpoint",
        "get_humidity",
    ]

    entities = []
    extra_status_attributes = False
    device = HomeAssistant.data[DOMAIN][config_entry.entry_id]
    if all(attribute in device.get_attributes for attribute in hvac_basic_attributes):
        if all(
            attribute in device.get_attributes
            for attribute in hvac_temperature_attributes
        ):
            supported_features |= ClimateEntityFeature.TARGET_TEMPERATURE
        if all(
            attribute in device.get_attributes for attribute in hvac_preset_attributes
        ):
            supported_features |= ClimateEntityFeature.PRESET_MODE
        if all(
            attribute in device.get_attributes for attribute in hvac_humidity_attributes
        ):
            supported_features |= ClimateEntityFeature.TARGET_HUMIDITY
        if all(
            attribute in device.get_attributes
            for attribute in hvac_extra_status_attributes
        ):
            extra_status_attributes = True
        entities.append(
            NilanClimate(device, supported_features, extra_status_attributes)
        )
    async_add_entities(entities, True)


class NilanClimate(NilanEntity, ClimateEntity, RestoreEntity):
    """Define a Nilan HVAC."""

    def __init__(self, device, supported_featrures, extra_status_attributes) -> None:
        """Init the class."""
        super().__init__(device)
        self._attr_translation_key = "hvac"
        self._attr_has_entity_name = True
        self._attr_unique_id = "hvac"
        self._hvac_on = False
        self._attr_max_humidity = 45
        self._attr_min_humidity = 15
        self._attr_min_temp = REAL_TEMP_MIN
        self._attr_max_temp = REAL_TEMP_MAX
        self._attr_target_temperature_step = REAL_TEMP_STEP
        self._target_real = None   # consigne réelle voulue (échelle T10)
        self._offset = None        # T_régulation - T10
        self._device_setpoint = None
        self._enable_turn_on_off_backwards_compatibility = False
        self._attr_hvac_modes = [
            HVACMode.COOL,
            HVACMode.HEAT,
            HVACMode.AUTO,
            HVACMode.OFF,
        ]
        self._attr_preset_modes = ["energy", "comfort", "water"]
        self._attr_fan_modes = ["0", "1", "2", "3", "4"]
        self._attr_temperature_unit = UnitOfTemperature.CELSIUS
        self._attr_supported_features = supported_featrures
        self._extra_status_attributes = extra_status_attributes

    async def async_set_fan_mode(self, fan_mode):
        """Set new target fan mode."""
        await self._device.set_ventilation_step(int(fan_mode))
        self.async_write_ha_state()

    async def async_set_preset_mode(self, preset_mode):
        """Set new target preset mode."""
        await self._device.set_air_exchange_mode(PRESET_TO_HVAC.get(preset_mode))
        self.async_write_ha_state()

    async def async_set_hvac_mode(self, hvac_mode):
        """Set new target operation mode."""
        if hvac_mode == HVACMode.OFF:
            await self._device.set_run_state(False)
        else:
            self._hvac_on = await self._device.get_run_state()
            if not self._hvac_on:
                await self._device.set_run_state(True)
            await self._device.set_operation_mode(STATE_TO_HVAC_MODE.get(hvac_mode))
        self._attr_hvac_mode = hvac_mode
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        """Restore the real (T10-scale) target after a restart."""
        await super().async_added_to_hass()
        last = await self.async_get_last_state()
        if last is not None:
            # attribut dédié : l'ancienne "temperature" était sur l'échelle T15
            value = last.attributes.get("consigne_reelle")
            if isinstance(value, (int, float)):
                self._target_real = float(value)

    def _device_target(self) -> int | None:
        """Setpoint to send to the controller for the current real target."""
        if self._target_real is None or self._offset is None:
            return None
        return int(max(5, min(30, round(self._target_real + self._offset))))

    async def async_set_temperature(self, **kwargs):
        """Set new real target temperature (T10 scale)."""
        self._target_real = float(kwargs[ATTR_TEMPERATURE])
        self._attr_target_temperature = self._target_real
        target = self._device_target()
        if target is not None:
            await self._device.set_user_temperature_setpoint(target)
            self._device_setpoint = target
        self.async_write_ha_state()

    @property
    def extra_state_attributes(self):
        """Expose the compensation internals."""
        return {
            "consigne_reelle": self._target_real,
            "consigne_envoyee_nilan": self._device_setpoint,
            "ecart_regulation_t10": None if self._offset is None else round(self._offset, 1),
        }

    async def async_set_humidity(self, humidity):
        """Set new target temperature."""
        await self._device.set_user_humidity_setpoint(humidity)
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Update sensor values."""
        self._hvac_on = await self._device.get_run_state()
        self._attr_fan_mode = str(await self._device.get_ventilation_step())

        if self._attr_supported_features & ClimateEntityFeature.TARGET_TEMPERATURE:
            self._device_setpoint = await self._device.get_user_temperature_setpoint()
            control_temp = await self._device.get_control_temperature()
            t10 = await self._device.get_t10_external_temperature()
            if control_temp is not None and t10 is not None:
                self._offset = control_temp - t10
                self._attr_current_temperature = t10
                if self._target_real is None and self._device_setpoint is not None:
                    # premier démarrage : on reprend la consigne actuelle de la carte
                    self._target_real = round(
                        (self._device_setpoint - self._offset) / REAL_TEMP_STEP
                    ) * REAL_TEMP_STEP
                self._attr_target_temperature = self._target_real
                wanted = self._target_real + self._offset
                if (
                    self._device_setpoint is not None
                    and abs(wanted - self._device_setpoint) > REWRITE_HYSTERESIS
                ):
                    target = self._device_target()
                    await self._device.set_user_temperature_setpoint(target)
                    self._device_setpoint = target
            else:
                # sondes indisponibles : comportement d'origine
                self._attr_target_temperature = self._device_setpoint
                self._attr_current_temperature = control_temp

        if self._attr_supported_features & ClimateEntityFeature.TARGET_HUMIDITY:
            self._attr_current_humidity = await self._device.get_humidity()
            self._attr_target_humidity = await self._device.get_user_humidity_setpoint()

        if self._attr_supported_features & ClimateEntityFeature.PRESET_MODE:
            self._attr_preset_mode = HVAC_TO_PRESET.get(
                await self._device.get_air_exchange_mode()
            )

        control_state = await self._device.get_control_state()
        if control_state is None:
            return

        if self._extra_status_attributes:
            ventilation_state = await self._device.get_ventilation_state()
            fan_supply_level = await self._device.get_supply_fan_level()
            if ventilation_state == 3:
                self._attr_hvac_action = HVACAction.DRYING
            elif control_state in (7, 17):
                self._attr_hvac_action = HVACAction.HEATING
            elif control_state in (8, 11):
                self._attr_hvac_action = HVACAction.COOLING
            elif fan_supply_level > 0:
                self._attr_hvac_action = HVACAction.FAN
            elif fan_supply_level == 0 and control_state > 0:
                self._attr_hvac_action = HVACAction.IDLE
            else:
                self._attr_hvac_action = HVACAction.OFF
        elif control_state in (7, 17):
            self._attr_hvac_action = HVACAction.HEATING
        elif control_state in (8, 11):
            self._attr_hvac_action = HVACAction.COOLING
        else:
            self._attr_hvac_action = HVACAction.OFF

        if not self._hvac_on:
            self._attr_hvac_mode = HVACAction.OFF
        else:
            self._attr_hvac_mode = HVAC_MODE_TO_STATE.get(
                await self._device.get_operation_mode()
            )
