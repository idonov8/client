import importlib
import types

def lazy_load(module_name, source_package=None, callback=None):
    if source_package is None:
        # TODO: need to have a map for commonly used imports here. Also handle dots
        source_package = module_name
    return LazyModule(module_name, source_package, callback)


class LazyModule(types.ModuleType):
    """Proxy module that lazily imports the underlying module the first time it
    is actually used.

    Shoutout to voxel51 for this :)

    Args:
        module_name: the fully-qualified module name to import
        callback (None): a callback function to call before importing the
            module
    """

    def __init__(self, module_name, source_package, callback=None):
        super().__init__(module_name)
        self._module = None
        self._callback = callback
        self._source_package = source_package

    def __getattr__(self, item):
        if self._module is None:
            self._import_module()

        return getattr(self._module, item)

    def __dir__(self):
        if self._module is None:
            self._import_module()

        return dir(self._module)

    def _import_module(self):
        # Execute callback, if any
        if self._callback is not None:
            self._callback()

        # Actually import the module
        try:
            module = importlib.import_module(self.__name__)
            self._module = module
        except ModuleNotFoundError:
            print(f"Could not import module {self.__name__}. Make sure to pip install {self._source_package}")
            raise

        # Update this object's dict so that attribute references are efficient
        # (__getattr__ is only called on lookups that fail)
        self.__dict__.update(module.__dict__)
