# Importing the web package registers the local-only private-pack endpoints.
from awb.web import private_pack as _private_pack  # noqa: F401
from awb.web import resource_monitor as _resource_monitor  # noqa: F401
from awb.web import clarity_overlay as _clarity_overlay  # noqa: F401
from awb.web import research_console as _research_console  # noqa: F401
from awb.web import research_console_link as _research_console_link  # noqa: F401

# The web application uses the general-purpose deep engine directly. Do not mutate
# awb.core.cloud_orchestrator globally: that class remains a reusable lower-level
# transport/routing primitive and is independently tested. runtime_entry is wired
# to the checkpointed deep engine by checkpoint_runtime below.
from awb.core.deep_engine import DeepIterativeEngine
from awb.web import app as _app_module

_app_module.Orchestrator = DeepIterativeEngine

# Register checkpoint boundaries and controls after runtime_entry has installed its
# process-isolated routes. checkpoint_runtime replaces runtime_entry's local class
# reference with CheckpointedDeepIterativeEngine without changing core modules.
from awb.web import checkpoint_runtime as _checkpoint_runtime  # noqa: F401,E402

# Final UI layer: exactly one project list/system page, one project dashboard and
# one project setup page. It is installed last so it replaces the historical
# Control/Dashboard/Lab navigation without changing the durable runtime semantics.
from awb.web import runtime_entry as _runtime_entry  # noqa: E402
from awb.web.unified_control import install_unified_control  # noqa: E402

install_unified_control(_runtime_entry.control_app, _runtime_entry)
