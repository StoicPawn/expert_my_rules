# Importing the web package registers the local-only private-pack endpoints.
from awb.web import private_pack as _private_pack  # noqa: F401
from awb.web import resource_monitor as _resource_monitor  # noqa: F401
from awb.web import clarity_overlay as _clarity_overlay  # noqa: F401
from awb.web import research_console as _research_console  # noqa: F401
from awb.web import research_console_link as _research_console_link  # noqa: F401

# Keep every web/runtime entry point on the same orchestrator contract. The
# focused variant still hot-reloads cloud-burst policy at every model-call
# boundary, but additionally keeps reviewer -> worker recovery chains focused
# until objections are resolved or an explicit dependency/reframe closes them.
from awb.core import cloud_orchestrator as _cloud_module
from awb.core.focused_cloud_orchestrator import FocusedCloudAwareOrchestrator
from awb.web import app as _app_module

# runtime_entry imports CloudAwareOrchestrator after the awb.web package has been
# initialized, so updating the module symbol here makes both legacy app.py and the
# process-isolated autonomous runtime use the focused lifecycle without forking two
# implementations.
_cloud_module.CloudAwareOrchestrator = FocusedCloudAwareOrchestrator
_app_module.Orchestrator = FocusedCloudAwareOrchestrator
