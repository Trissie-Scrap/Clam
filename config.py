import yaml


# NOTE: This is not a config file
# This is only a helper class for the actual
# config file


class DebugMode:
    def __init__(self, mode):
        self.mode = mode

    def __bool__(self):
        return bool(self.mode)

    def __int__(self):
        return self.mode

    def __str__(self):
        return str(self.mode)

    @property
    def off(self):
        return self.mode == 0

    @property
    def partial(self):
        return self.mode == 1

    @property
    def full(self):
        return self.mode == 2


class Config:
    """config.yml helper class"""

    def __init__(self, file_path):
        self._file_path = file_path

        with open(file_path, "r") as config:
            self._data = yaml.safe_load(config)

        # Required config stuff
        self.bot_token = self._data["bot-token"]  # Bot token
        self.console = self._data["console"]  # Console channel ID
        self.reddit_id = self._data["reddit-id"]  # Reddit app ID
        self.reddit_secret = self._data["reddit-secret"]  # Reddit app secret
        self.google_api_key = self._data["google-api-key"]  # Google api key
        self.database_uri = self._data["database-uri"]  # Postgres database URI

        # Optional config stuff
        # Run the bot in debug mode or not
        # 0: Off | 1: Test acc | 2: Same acc
        self.debug = DebugMode(self._data["debug"] if "debug" in self._data else 0)
        # Webhook for status messages
        self.status_hook = self._data["status-hook"] if "status-hook" in self._data else None
