# Importing the web package registers the local-only private-pack endpoints.
from awb.web import private_pack as _private_pack  # noqa: F401
from awb.web import resource_monitor as _resource_monitor  # noqa: F401
from awb.web import clarity_overlay as _clarity_overlay  # noqa: F401
from awb.web import research_console as _research_console  # noqa: F401
from awb.web import research_console_link as _research_console_link  # noqa: F401
