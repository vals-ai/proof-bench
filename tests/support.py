import importlib
import sys
import types


def install_model_library_stub() -> None:
    if "model_library.base" not in sys.modules:
        base_module = types.ModuleType("model_library.base")

        class _TextInput:
            def __init__(self, text):
                self.text = text

        class _ToolResult:
            def __init__(self, tool_call=None, result=None):
                self.tool_call = tool_call
                self.result = result

        class _ToolBody:
            def __init__(self, **kwargs):
                for key, value in kwargs.items():
                    setattr(self, key, value)

        class _ToolDefinition:
            def __init__(self, name, body):
                self.name = name
                self.body = body

        base_module.TextInput = _TextInput
        base_module.ToolResult = _ToolResult
        base_module.ToolBody = _ToolBody
        base_module.ToolDefinition = _ToolDefinition
        sys.modules["model_library.base"] = base_module
    else:
        base_module = sys.modules["model_library.base"]

    if "model_library.registry_utils" not in sys.modules:
        registry_module = types.ModuleType("model_library.registry_utils")
        registry_module.get_registry_model = lambda *args, **kwargs: None
        sys.modules["model_library.registry_utils"] = registry_module
    else:
        registry_module = sys.modules["model_library.registry_utils"]
        if not hasattr(registry_module, "get_registry_model"):
            registry_module.get_registry_model = lambda *args, **kwargs: None

    if "model_library" not in sys.modules:
        root_module = types.ModuleType("model_library")
        sys.modules["model_library"] = root_module
    else:
        root_module = sys.modules["model_library"]

    root_module.base = base_module
    root_module.registry_utils = registry_module


def install_vals_sdk_stub() -> None:
    if "vals.sdk.types" in sys.modules:
        return

    vals_module = types.ModuleType("vals")
    sdk_module = types.ModuleType("vals.sdk")
    types_module = types.ModuleType("vals.sdk.types")
    types_module.OutputObject = type("OutputObject", (), {})
    vals_module.sdk = sdk_module
    sdk_module.types = types_module
    sys.modules["vals"] = vals_module
    sys.modules["vals.sdk"] = sdk_module
    sys.modules["vals.sdk.types"] = types_module


def reload_module(module_name: str):
    module = importlib.import_module(module_name)
    return importlib.reload(module)
