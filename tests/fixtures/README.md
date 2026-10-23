# Home Assistant flow API fixture

`homeassistant_config_entries_2026_10_2.py` contains seven methods extracted from
[Home Assistant Core 2026.10.2 `homeassistant/config_entries.py`](https://github.com/home-assistant/core/blob/2026.10.2/homeassistant/config_entries.py).
They are upstream Home Assistant code under the **Apache License 2.0**; a copy is
included as `LICENSE-APACHE-2.0`. The repository's MIT license does not replace
this fixture's upstream license.

The extraction removes source imports, unrelated classes/methods and runtime
initialization. The selected method bodies and decorators remain unchanged.
The original cached complete source SHA-256 was
`221e2672e9a8f0ec12fa62edbfdc2ef6d986331855797615c81ecbe99f8840db`.

The camera reconfiguration tests parse this fixture and execute only the selected
methods in a controlled namespace with local HA fakes. This checks the pinned
native update/reconfigure semantics without installing or running Home Assistant.
It does not claim to be an end-to-end Home Assistant compatibility test.
