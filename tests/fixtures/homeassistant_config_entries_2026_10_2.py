# SPDX-License-Identifier: Apache-2.0
# Extracted from Home Assistant Core 2026.10.2, homeassistant/config_entries.py.
# Source: https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/config_entries.py
# Upstream project: Home Assistant contributors.
# Publication change: retain only seven ConfigFlow methods, without imports,
# other methods or runtime initialization. Method bodies/decorators are unchanged.
# This file is parsed as a test fixture, not imported as a runnable HA module.
# See README.md and LICENSE-APACHE-2.0 in this directory.

class ConfigFlow:
    @property
    def unique_id(self) -> str | None:
        """Return unique ID if available."""
        if not self.context:
            return None

        return self.context.get("unique_id")


    async def async_set_unique_id(
        self, unique_id: str | None = None, *, raise_on_progress: bool = True
    ) -> ConfigEntry | None:
        """Set a unique ID for the config flow.

        Returns optionally existing config entry with same ID.
        """
        if unique_id is None:
            self.context["unique_id"] = None
            return None

        if raise_on_progress:
            if any(
                flow["context"]["source"] != SOURCE_REAUTH
                for flow in self._async_in_progress(
                    include_uninitialized=True, match_context={"unique_id": unique_id}
                )
            ):
                raise data_entry_flow.AbortFlow(
                    "already_in_progress", translation_domain=HOMEASSISTANT_DOMAIN
                )

        self.context["unique_id"] = unique_id

        # Abort discoveries done using the default discovery unique id
        if unique_id != DEFAULT_DISCOVERY_UNIQUE_ID:
            for progress in self._async_in_progress(
                include_uninitialized=True,
                match_context={"unique_id": DEFAULT_DISCOVERY_UNIQUE_ID},
            ):
                self.hass.config_entries.flow.async_abort(progress["flow_id"])

        return self.hass.config_entries.async_entry_for_domain_unique_id(
            self.handler, unique_id
        )


    @callback
    def _async_current_entries(
        self, include_ignore: bool | None = None
    ) -> list[ConfigEntry]:
        """Return current entries.

        If the flow is user initiated, filter out ignored entries,
        unless include_ignore is True.
        """
        return self.hass.config_entries.async_entries(
            self.handler,
            include_ignore or (include_ignore is None and self.source != SOURCE_USER),
        )


    @callback
    def __async_update(
        self,
        entry: ConfigEntry,
        *,
        unique_id: str | UndefinedType | None,
        title: str | UndefinedType,
        data: Mapping[str, Any] | UndefinedType,
        data_updates: Mapping[str, Any] | UndefinedType,
        options: Mapping[str, Any] | UndefinedType,
    ) -> bool:
        """Update config entry and return result.

        Internal to be used by update_and_abort and
        update_reload_and_abort methods only.
        """

        if data_updates is not UNDEFINED:
            if data is not UNDEFINED:
                raise ValueError("Cannot set both data and data_updates")
            data = entry.data | data_updates
        return self.hass.config_entries.async_update_entry(
            entry=entry,
            unique_id=unique_id,
            title=title,
            data=data,
            options=options,
        )


    @callback
    def async_update_and_abort(
        self,
        entry: ConfigEntry,
        *,
        unique_id: str | UndefinedType | None = UNDEFINED,
        title: str | UndefinedType = UNDEFINED,
        data: Mapping[str, Any] | UndefinedType = UNDEFINED,
        data_updates: Mapping[str, Any] | UndefinedType = UNDEFINED,
        options: Mapping[str, Any] | UndefinedType = UNDEFINED,
        reason: str | UndefinedType = UNDEFINED,
    ) -> ConfigFlowResult:
        """Update config entry and finish config flow.

        Args:
            entry: config entry to update
            unique_id: replace the unique_id of the entry
            title: replace the title of the entry
            data: replace the entry data with new data
            data_updates: add items from data_updates to entry data - existing keys
                are overridden
            options: replace the entry options with new options
            reason: set the reason for the abort, defaults to
                `reauth_successful` or `reconfigure_successful` based on flow source.
                A custom reason requires a matching strings.json entry

        Returns:
            ConfigFlowResult: The result of the config flow.
        """
        self.__async_update(
            entry=entry,
            unique_id=unique_id,
            title=title,
            data=data,
            data_updates=data_updates,
            options=options,
        )
        translation_domain: str | None = None
        if reason is UNDEFINED:
            if self.source == SOURCE_RECONFIGURE:
                reason = "reconfigure_successful"
            else:
                reason = "reauth_successful"
            translation_domain = HOMEASSISTANT_DOMAIN
        return self.async_abort(reason=reason, translation_domain=translation_domain)


    @property
    def _reconfigure_entry_id(self) -> str:
        """Return reconfigure entry id."""
        if self.source != SOURCE_RECONFIGURE:
            raise ValueError(f"Source is {self.source}, expected {SOURCE_RECONFIGURE}")
        return self.context["entry_id"]


    @callback
    def _get_reconfigure_entry(self) -> ConfigEntry:
        """Return the reconfigure config entry linked to the current context."""
        return self.hass.config_entries.async_get_known_entry(
            self._reconfigure_entry_id
        )

