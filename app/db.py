import datetime
import json
from pathlib import Path

from peewee import (
    CharField,
    DateTimeField,
    Model,
    SqliteDatabase,
    TextField,
)

from app.config import config

config.ensure_dirs()
db = SqliteDatabase(str(config.CONFIG_DIR / "app.db"))


class BaseModel(Model):
    class Meta:
        database = db


class Job(BaseModel):
    # Statuses, in the order a job normally moves through them:
    #   pending -> queued -> detecting -> awaiting_metadata_confirm
    #            -> processing -> done
    # Any state can transition to "failed".
    STATUS_PENDING = "pending"
    STATUS_QUEUED = "queued"
    STATUS_DETECTING = "detecting"
    STATUS_AWAITING_METADATA_CONFIRM = "awaiting_metadata_confirm"
    STATUS_PROCESSING = "processing"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"

    source_path = CharField(index=True)
    source_type = CharField(null=True)  # m4b_single | mp3_multi | mp3_single
    audio_files_json = TextField(null=True)
    status = CharField(default=STATUS_PENDING)

    title_guess = CharField(null=True)
    author_guess = CharField(null=True)

    candidates_json = TextField(null=True)
    selected_metadata_json = TextField(null=True)

    destination_path = CharField(null=True)
    error_message = TextField(null=True)
    log = TextField(default="")

    created_at = DateTimeField(default=datetime.datetime.utcnow)
    updated_at = DateTimeField(default=datetime.datetime.utcnow)

    def append_log(self, line: str):
        timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        self.log = f"{self.log}[{timestamp}] {line}\n"
        self.touch_and_save()

    def touch_and_save(self):
        self.updated_at = datetime.datetime.utcnow()
        self.save()

    @property
    def candidates(self):
        return json.loads(self.candidates_json) if self.candidates_json else []

    @candidates.setter
    def candidates(self, value):
        self.candidates_json = json.dumps(value)

    @property
    def audio_files(self):
        return [Path(p) for p in json.loads(self.audio_files_json)] if self.audio_files_json else []

    @audio_files.setter
    def audio_files(self, paths):
        self.audio_files_json = json.dumps([str(p) for p in paths])

    @property
    def selected_metadata(self):
        return json.loads(self.selected_metadata_json) if self.selected_metadata_json else None

    @selected_metadata.setter
    def selected_metadata(self, value):
        self.selected_metadata_json = json.dumps(value) if value is not None else None


def init_db():
    db.connect(reuse_if_open=True)
    db.create_tables([Job])
