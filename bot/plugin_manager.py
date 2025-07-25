
class PluginManager:
    def __init__(self, config: dict):
        self.config = config

    def get_functions_specs(self):
        return []

    def call_function(self, name, openai_helper, args):
        raise NotImplementedError

    def get_plugin_source_name(self, plugin):
        return plugin
