class PluginManager:
    def __init__(self, config: dict):
        self.config = config
    def get_functions_specs(self):
        return []
    async def call_function(self, name, helper, args):
        raise NotImplementedError
