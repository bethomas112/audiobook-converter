import datetime
import json
from pathlib import Path

from peewee import (
    BooleanField,
    CharField,
    DateTimeField,
    IntegerField,
    Model,
    SqliteDatabase,
    TextField,
)

from app.config import config

config.ensure_dirs()
# WAL mode matters here specifically because two separate OS processes (the
# web server and the Huey worker) read and write this same database file.
# In SQLite's default rollback-journal mode, a connection's read can miss
# another connection's already-committed writes until that connection
# starts a fresh transaction of its own - which is exactly what caused
# /api/status polls to serve a stale job status while the worker was
# actively writing live progress updates to the same row. WAL is the
# standard fix for cross-process read/write visibility like this.
db = SqliteDatabase(
    str(config.CONFIG_DIR / "app.db"),
    pragmas={"journal_mode": "wal", "synchronous": "normal"},
)


class BaseModel(Model):
    class Meta:
        database = db


class Job(BaseModel):
    # Statuses, in the order a job normally moves through them:
    #   pending -> queued -> detecting -> awaiting_metadata_confirm
    #            -> ready -> processing -> done
    # "ready" means metadata is confirmed and the job is waiting its turn in
    # the conversion queue; only the job the dispatcher has actually started
    # is "processing" - at most one job holds that status at a time, since
    # conversions run strictly one at a time. Any state can transition to
    # "failed"; "ready" and "processing" can transition to "cancelled",
    # which returns the job to the needs-input group with its confirmed
    # metadata intact, ready to be queued again.
    STATUS_PENDING = "pending"
    STATUS_QUEUED = "queued"
    STATUS_DETECTING = "detecting"
    STATUS_AWAITING_METADATA_CONFIRM = "awaiting_metadata_confirm"
    STATUS_READY = "ready"
    STATUS_PROCESSING = "processing"
    STATUS_DONE = "done"
    STATUS_FAILED = "failed"
    STATUS_CANCELLED = "cancelled"

    source_path = CharField(index=True)
    source_type = CharField(null=True)  # m4b_single | mp3_multi | mp3_single
    audio_files_json = TextField(null=True)
    status = CharField(default=STATUS_PENDING)

    title_guess = CharField(null=True)
    author_guess = CharField(null=True)

    candidates_json = TextField(null=True)
    selected_metadata_json = TextField(null=True)
    chapters_preview_json = TextField(null=True)

    # Manual ordering within the conversion queue (ready + the currently
    # processing job). Null for jobs not in that queue. Reordering just
    # rewrites these values - nothing is ever eagerly enqueued in Huey until
    # the dispatcher actually starts a job, so "moving" a queued job is a
    # plain DB update, not a queue-system operation.
    queue_order = IntegerField(null=True)

    progress_pct = IntegerField(default=0)
    progress_stage = CharField(null=True)
    cancel_requested = BooleanField(default=False)

    # "Removing" a job hides it rather than deleting its row: the watcher
    # dedupes new inbox arrivals against every historical job's source_path
    # (see app/watcher.py), so deleting the row for a job whose source file
    # is still sitting in the inbox would make the watcher re-detect it as
    # a brand new drop-off.
    dismissed = BooleanField(default=False)

    destination_path = CharField(null=True)
    error_message = TextField(null=True)
    log = TextField(default="")

    created_at = DateTimeField(default=datetime.datetime.utcnow)
    updated_at = DateTimeField(default=datetime.datetime.utcnow)

    def append_log(self, line: str):
        """Narrow save (log + updated_at only) for the same reason
        save_progress uses one: process_job holds a single Job instance for
        a job's entire run and calls this repeatedly as the pipeline
        progresses. A full save() here would re-persist whatever that
        instance's other fields looked like when it was first loaded -
        clobbering any out-of-band update to them made since, most
        importantly cancel_requested, which cancel_job() sets directly in
        the DB from the *other* process while a conversion is running.
        """
        timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        self.log = f"{self.log}[{timestamp}] {line}\n"
        self.updated_at = datetime.datetime.utcnow()
        self.save(only=[Job.log, Job.updated_at])

    def touch_and_save(self):
        self.updated_at = datetime.datetime.utcnow()
        self.save()

    def save_progress(self, pct: int, stage: str | None = None):
        """Lightweight, frequent-safe progress update - writes only the
        progress columns so it can't clobber fields another save() call
        touched in between (e.g. a concurrently-appended log line).
        """
        self.progress_pct = pct
        if stage is not None:
            self.progress_stage = stage
        self.updated_at = datetime.datetime.utcnow()
        fields = [Job.progress_pct, Job.updated_at] + ([Job.progress_stage] if stage is not None else [])
        self.save(only=fields)

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
    def chapters_preview(self):
        return json.loads(self.chapters_preview_json) if self.chapters_preview_json else None

    @chapters_preview.setter
    def chapters_preview(self, value):
        self.chapters_preview_json = json.dumps(value) if value is not None else None

    @property
    def selected_metadata(self):
        return json.loads(self.selected_metadata_json) if self.selected_metadata_json else None

    @selected_metadata.setter
    def selected_metadata(self, value):
        self.selected_metadata_json = json.dumps(value) if value is not None else None


def init_db():
    db.connect(reuse_if_open=True)
    db.create_tables([Job])
