# Importing the web package registers the local-only private-pack endpoints.
from awb.web import private_pack as _private_pack  # noqa: F401
from awb.web import resource_monitor as _resource_monitor  # noqa: F401
from awb.web import clarity_overlay as _clarity_overlay  # noqa: F401
from awb.web import research_console as _research_console  # noqa: F401
from awb.web import research_console_link as _research_console_link  # noqa: F401

# Keep every web/runtime entry point on the same general-purpose engine contract.
# Logical roles are sequential views over one durable micro-task state machine;
# this is a redesign of the execution core, not another agent layer on the legacy
# select->execute->review pipeline.
from awb.core import cloud_orchestrator as _cloud_module
from awb.core.deep_engine import DeepIterativeEngine
from awb.web import app as _app_module

_cloud_module.CloudAwareOrchestrator = DeepIterativeEngine
_app_module.Orchestrator = DeepIterativeEngine

# Register checkpoint boundaries and controls after runtime_entry has installed its
# process-isolated routes. checkpoint_runtime wraps DeepIterativeEngine without
# changing its task-graph semantics.
from awb.web import checkpoint_runtime as _checkpoint_runtime  # noqa: F401,E402
