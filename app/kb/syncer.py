
class KBSyncer:
    def __init__(self, yandex_client, embedder, kb_repo, settings):
        self.yd = yandex_client
        self.embedder = embedder
        self.kb = kb_repo
        self.settings = settings
    def sync(self):
        # TODO: list from Yandex, parse & chunk, embed & upsert to KBRepo
        return {"status":"ok","indexed":0}
