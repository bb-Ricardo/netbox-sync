from hcloud import Client


class HetznerClient:

    def __init__(self, token):
        self.client = Client(token=token)

    def get_servers(self):
        return self.client.servers.get_all()
