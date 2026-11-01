from pydantic import BaseModel

from bot.database import MongoDB


class SettingsModel(BaseModel):
    """
    A model representing the bot's settings.

    Parameters:
        FORCE_SUB_MESSAGE (str | int): The message to display when a user
            is not subscribed.
        BANNED_USER_MESSAGE (str | int): The message to display if user
            is banned from using the bot.
        START_MESSAGE (str | int): The message to display when a user
            starts the bot.
        ADDITIONAL_MESSAGE (str | int): The message to display after a
            user received a file, set to 0 to disable.
        USER_REPLY_TEXT (str | int): The text to reply to a user.
        CUSTOM_CAPTION (str | int): Custom caption to every file sent,
            set 0 to disable.
        AUTO_DELETE_MESSAGE (str | int): The message to display when
            a file is deleted.
        INVALID_LINK_MESSAGE (str | int): The message to display when
            a file link is invalid.
        FILE_DOES_NOT_EXIST (str | int): The message to display when a
            file does not exist from codexbotz links.

        AUTO_DELETE_SECONDS (int): Number of seconds to wait before
            deleting a file, set to 0 to disable.
        GLOBAL_MODE (bool): Whether the bot is in global mode.
        BACKUP_FILES (bool): Whether to backup files.

        GBCAST_GROUP_ID (int): Chat ID /gbcast posts announcements to,
            set to 0 to disable.
        GBCAST_TEMPLATE (str): Preset announcement text for /gbcast.
        GBCAST_COOLDOWN_SECONDS (int): Minimum seconds between two
            /gbcast uses by the same admin.

        DAILY_FILE_LIMIT (int): Maximum file links a non-admin user
            may request.
        DAILY_FILE_LIMIT_WINDOW_SECONDS (int): Length of the rolling
            window for DAILY_FILE_LIMIT.
        RATE_LIMIT_PER_MINUTE (int): Maximum command executions per
            minute allowed from the same chat.

        MPOST_DEFAULT_PRESET (str | int): Name of the /preset_save
            preset that /mpost uses by default for its button layout.
            Set to 0 for none.
    """

    FORCE_SUB_MESSAGE: str | int = "Please join the channel(s) first."
    BANNED_USER_MESSAGE: str | int = (
        "You have been banned from using this bot."
    )
    START_MESSAGE: str | int = "I am a file-sharing bot."
    ADDITIONAL_MESSAGE: str | int = 0
    USER_REPLY_TEXT: str | int = "idk"

    CUSTOM_CAPTION: str | int = 0
    AUTO_DELETE_MESSAGE: str | int = (
        "This file(s) will be deleted within {} minutes"
    )
    INVALID_LINK_MESSAGE: str | int = (
        "Attempted to resolve link: Got invalid link."
    )
    FILE_DOES_NOT_EXIST: str | int = (
        "Attempted to fetch files: Does not exist."
    )

    AUTO_DELETE_SECONDS: int = 300
    GLOBAL_MODE: bool = False
    BACKUP_FILES: bool = True

    GBCAST_GROUP_ID: int = 0

    GBCAST_TEMPLATE: str = (
        '[] has been uploaded in the respective channel.\n\n'
        'Type Movie "[]" to get the movie.'
    )

    GBCAST_COOLDOWN_SECONDS: int = 30

    DAILY_FILE_LIMIT: int = 20
    DAILY_FILE_LIMIT_WINDOW_SECONDS: int = 86400
    RATE_LIMIT_PER_MINUTE: int = 25

    # Name of the /preset_save preset used by /mpost.
    # 0 = no default preset.
    MPOST_DEFAULT_PRESET: str | int = 0


class InvalidValueError(Exception):
    """
    Raised when a value has an invalid type for the requested setting.
    """

    def __init__(self, key: str | int) -> None:
        super().__init__(
            f"Value for key '{key}' must have the same type "
            "as the existing value."
        )


class Options(MongoDB):
    """
    Handles loading and updating bot settings.
    """

    def __init__(self) -> None:
        super().__init__()

        self.settings = SettingsModel()
        self.collection = "BotSettings"
        self.document_id = "MainOptions"

    async def load_settings(self) -> None:
        """
        Load settings from MongoDB.

        Settings are stored in:
            BotSettings
        with:
            _id = MainOptions
        """

        pipeline = [
            {
                "$match": {
                    "_id": self.document_id,
                },
            },
        ]

        cursor = self.db[self.collection].aggregate(pipeline)
        settings_doc = await cursor.to_list(length=None)

        if settings_doc:
            self.settings = SettingsModel(**settings_doc[0])
        else:
            self.settings = SettingsModel()

        # Ensure the document exists with all default/current fields.
        update = {
            "$set": self.settings.model_dump(),
        }

        db_filter = {
            "_id": self.document_id,
        }

        await self.db[self.collection].update_one(
            filter=db_filter,
            update=update,
            upsert=True,
        )

    async def update_settings(
        self,
        key: str,
        value: str | int | bool,
    ) -> SettingsModel:
        """
        Update one setting and save it to MongoDB.
        """

        if key not in SettingsModel.model_fields:
            raise KeyError(key)

        field_info = SettingsModel.model_fields[key]

        # Pydantic stores the annotation as a union for fields such as:
        # str | int
        annotation = field_info.annotation

        if annotation is not None:
            try:
                valid_type = isinstance(value, annotation)
            except TypeError:
                # Fallback for unusual typing annotations.
                valid_type = True

            if not valid_type:
                raise InvalidValueError(key)

        setattr(self.settings, key, value)

        # Re-validate the complete settings model.
        self.settings = SettingsModel(
            **self.settings.model_dump(),
        )

        model_key = key
        model_value = getattr(self.settings, key)

        db_filter = {
            "_id": self.document_id,
        }

        update = {
            "$set": {
                model_key: model_value,
            },
        }

        await self.db[self.collection].update_one(
            filter=db_filter,
            update=update,
            upsert=True,
        )

        return self.settings


# Global options instance used throughout the bot.
options = Options()
